#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AdsPower Profile Monitor -> Telegram Notifier
================================================

Отслеживает группы профилей в AdsPower и отправляет уведомления
в Telegram при появлении новых профилей.

Оптимизирован для работы 24/7:
- автоматическое переподключение при сбоях
- проверка пропущенных профилей при запуске (добавленных, пока бот не работал)
- ограничение размера журнала в памяти

Требования:
    pip install requests

Запуск:
    python adspower_monitor.py
"""

import json
import os
import platform
import sys
import time
import threading
import traceback
from datetime import datetime

import requests

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
SEEN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen_profiles.json")

MAX_LOG_LINES = 5000  # максимум строк в журнале (для экономии RAM при 24/7)
MAX_RECONNECT_DELAY = 300  # максимум 5 минут между попытками переподключения

DEFAULT_CONFIG = {
    "api_base": "http://local.adspower.net:50325",
    "api_key": "",
    "telegram_token": "",
    "telegram_chat_id": "",
    "groups": [],          # [{name: "...", tag: "..."}, ...]
    "poll_interval": 30,
}


# ----------------------------------------------------------------------
# Config load/save with auto-migration from old format
# ----------------------------------------------------------------------
def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            merged = dict(DEFAULT_CONFIG)
            merged.update(cfg)
            # Auto-migrate: old format had groups as a string
            if isinstance(merged.get("groups"), str):
                merged["groups"] = _migrate_groups_string(merged["groups"])
                # Remove demo_mode if it exists from old config
                merged.pop("demo_mode", None)
                save_config(merged)
            return merged
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)


def _migrate_groups_string(text):
    """Convert old 'group1 | tag1\\ngroup2' string format to list of dicts."""
    result = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if "|" in line:
            name, tag = line.split("|", 1)
            result.append({"name": name.strip(), "tag": tag.strip()})
        else:
            result.append({"name": line, "tag": ""})
    return result


def save_config(cfg):
    # Don't save demo_mode if it somehow still exists
    cfg_copy = {k: v for k, v in cfg.items() if k != "demo_mode"}
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg_copy, f, ensure_ascii=False, indent=2)


# ----------------------------------------------------------------------
# Persistent "seen profiles" storage
# ----------------------------------------------------------------------
def load_seen():
    if os.path.exists(SEEN_PATH):
        try:
            with open(SEEN_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_seen(seen):
    tmp_path = SEEN_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, SEEN_PATH)


# ----------------------------------------------------------------------
# AdsPower API client
# ----------------------------------------------------------------------
class AdsPowerClient:
    MIN_REQUEST_INTERVAL = 1.1  # seconds between requests (API rate limit)

    def __init__(self, api_base, api_key=""):
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key.strip()
        self._last_request_time = 0.0
        self._session = requests.Session()

    def _throttle(self):
        elapsed = time.time() - self._last_request_time
        wait = self.MIN_REQUEST_INTERVAL - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_request_time = time.time()

    def _get(self, path, params=None):
        params = dict(params or {})
        if self.api_key:
            params["api_key"] = self.api_key
        url = f"{self.api_base}{path}"

        last_error = None
        for attempt in range(3):
            self._throttle()
            try:
                resp = self._session.get(url, params=params, timeout=15)
                resp.raise_for_status()
            except requests.exceptions.RequestException as e:
                last_error = e
                time.sleep(1.5 * (attempt + 1))
                continue
            data = resp.json()
            msg = str(data.get("msg", ""))
            if str(data.get("code")) not in ("0", "success", "200"):
                if "too many request" in msg.lower() or "too frequent" in msg.lower():
                    last_error = RuntimeError(f"AdsPower API error on {path}: {msg}")
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise RuntimeError(f"AdsPower API error on {path}: {msg}")
            return data.get("data", {})
        raise last_error

    def list_groups(self):
        """Returns list of all groups: [{'group_id':.., 'group_name':..}, ...]"""
        groups = []
        page = 1
        page_size = 100
        while True:
            data = self._get("/api/v1/group/list", {"page": page, "page_size": page_size})
            items = data.get("list", []) or []
            groups.extend(items)
            if len(items) < page_size:
                break
            page += 1
        return groups

    def list_profiles(self, group_id):
        """Returns list of profiles in a group: [{'user_id':.., 'name':..}, ...]"""
        profiles = []
        page = 1
        page_size = 100
        while True:
            data = self._get("/api/v1/user/list", {"group_id": group_id, "page": page, "page_size": page_size})
            items = data.get("list", []) or []
            profiles.extend(items)
            if len(items) < page_size:
                break
            page += 1
        return profiles


# ----------------------------------------------------------------------
# Telegram client
# ----------------------------------------------------------------------
class TelegramClient:
    def __init__(self, token, chat_id):
        self.token = token.strip()
        self.chat_id = chat_id.strip()
        self._session = requests.Session()

    def send_message(self, text, parse_mode=None):
        if not self.token or not self.chat_id:
            raise RuntimeError("Не заполнены токен бота или chat_id Telegram")
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        data = {"chat_id": self.chat_id, "text": text}
        if parse_mode:
            data["parse_mode"] = parse_mode
        resp = self._session.post(url, data=data, timeout=15)
        resp.raise_for_status()
        result = resp.json()
        if not result.get("ok"):
            raise RuntimeError(f"Telegram API error: {result}")
        return result


# ----------------------------------------------------------------------
# Monitor worker (runs in background thread, optimized for 24/7)
# ----------------------------------------------------------------------
class Monitor:
    def __init__(self, config, log_fn, stop_event):
        self.config = config
        self.log = log_fn
        self.stop_event = stop_event
        self.seen = load_seen()

    def _build_message(self, group_name, profile_name, tag, timestamp_str):
        """Build notification message with optional tag."""
        base_text = (
            f"Группа: {group_name}\n"
            f"Добавлен новый профиль: {profile_name}\n"
            f"Время добавления: {timestamp_str}"
        )
        parse_mode = None
        if tag:
            if tag.startswith("@"):
                message = base_text + f"\nТег: {tag}"
            elif tag.lstrip("-").isdigit():
                message = base_text + f'\nТег: <a href="tg://user?id={tag}">тег</a>'
                parse_mode = "HTML"
            else:
                message = base_text + f"\nТег: {tag}"
        else:
            message = base_text
        return message, parse_mode

    def _send_with_retry(self, tg, message, parse_mode, max_retries=3):
        """Send Telegram message with retry on failure."""
        for attempt in range(max_retries):
            try:
                tg.send_message(message, parse_mode=parse_mode)
                return True
            except Exception as e:
                if attempt < max_retries - 1:
                    delay = 2 ** (attempt + 1)
                    self.log(f"⚠ Ошибка отправки в Telegram (попытка {attempt + 1}/{max_retries}): {e}")
                    time.sleep(delay)
                else:
                    self.log(f"❌ Не удалось отправить в Telegram после {max_retries} попыток: {e}")
                    return False

    def run(self):
        ads = AdsPowerClient(self.config["api_base"], self.config.get("api_key", ""))
        tg = TelegramClient(self.config["telegram_token"], self.config["telegram_chat_id"])

        groups_list = self.config.get("groups", [])
        if not groups_list:
            self.log("Не указано ни одной группы. Добавьте группы и запустите заново.")
            return

        wanted_names = [g["name"] for g in groups_list]
        tag_by_group = {g["name"]: g.get("tag", "") for g in groups_list}

        # --- Resolve groups from AdsPower ---
        self.log("Получаю список групп из AdsPower...")
        all_groups = self._fetch_groups_with_retry(ads)
        if all_groups is None:
            return

        name_to_id = {g.get("group_name"): g.get("group_id") for g in all_groups}
        self.log(f"AdsPower вернул {len(name_to_id)} групп: {list(name_to_id.keys())}")

        target_groups = {}
        is_all = any(w.strip().lower() in ['*', 'все', 'all'] for w in wanted_names)
        if is_all:
            # Find the wildcard tag (from the * row)
            wildcard_entry = next(g for g in groups_list if g["name"].strip().lower() in ['*', 'все', 'all'])
            wildcard_tag = wildcard_entry.get("tag", "")
            # Add ALL groups from AdsPower
            for name, gid in name_to_id.items():
                target_groups[name] = gid
                # Use specific tag if this group was also listed explicitly,
                # otherwise fall back to wildcard tag
                if name not in tag_by_group or tag_by_group[name] == "":
                    tag_by_group[name] = wildcard_tag
            self.log(f"Режим «все группы» (*): добавлено {len(target_groups)} групп")
        else:
            for wanted in wanted_names:
                if wanted in name_to_id:
                    target_groups[wanted] = name_to_id[wanted]
                else:
                    self.log(f"⚠ Группа '{wanted}' не найдена в AdsPower — проверьте название.")

        if not target_groups:
            self.log("Ни одна из указанных групп не найдена. Остановка.")
            return

        self.log(f"Слежу за группами ({len(target_groups)}): {', '.join(target_groups.keys())}")
        self.log(f"Интервал проверки: {self.config['poll_interval']} сек.")

        # --- Check for missed profiles (added while bot was offline) ---
        self._check_missed_profiles(ads, tg, target_groups, tag_by_group)

        # --- Main monitoring loop ---
        consecutive_errors = 0
        while not self.stop_event.is_set():
            try:
                self._poll_once(ads, tg, target_groups, tag_by_group)
                consecutive_errors = 0  # reset on success
            except Exception as e:
                consecutive_errors += 1
                delay = min(2 ** consecutive_errors, MAX_RECONNECT_DELAY)
                self.log(f"⚠ Ошибка в цикле мониторинга (попытка #{consecutive_errors}): {e}")
                self.log(f"   Повторная попытка через {delay} сек...")
                # Wait with stop_event check
                waited = 0
                while waited < delay and not self.stop_event.is_set():
                    time.sleep(1)
                    waited += 1
                continue

            # Wait for next poll interval
            waited = 0
            interval = max(5, int(self.config.get("poll_interval", 30)))
            while waited < interval and not self.stop_event.is_set():
                time.sleep(1)
                waited += 1

        self.log("Мониторинг остановлен.")

    def _fetch_groups_with_retry(self, ads):
        """Fetch groups list with retry logic. Returns None on permanent failure."""
        for attempt in range(5):
            if self.stop_event.is_set():
                return None
            try:
                return ads.list_groups()
            except Exception as e:
                delay = min(2 ** (attempt + 1), 60)
                self.log(f"⚠ Ошибка получения групп (попытка {attempt + 1}/5): {e}")
                if attempt < 4:
                    self.log(f"   Повторная попытка через {delay} сек...")
                    time.sleep(delay)
        self.log("❌ Не удалось получить список групп после 5 попыток. Остановка.")
        return None

    def _check_missed_profiles(self, ads, tg, target_groups, tag_by_group):
        """Check for profiles added while the bot was offline and notify about them."""
        missed_total = 0
        # If wildcard (*) is active and bot has run before (seen has data),
        # treat new groups' profiles as "missed" — notify about them all.
        bot_ran_before = len(self.seen) > 0
        is_wildcard = any(
            g["name"].strip().lower() in ['*', 'все', 'all']
            for g in self.config.get("groups", [])
        )

        for name, gid in target_groups.items():
            if self.stop_event.is_set():
                break
            gid_str = str(gid)
            try:
                profiles = ads.list_profiles(gid)
            except Exception as e:
                self.log(f"⚠ Ошибка при проверке группы '{name}': {e}")
                continue

            current_ids = {str(p.get("user_id")) for p in profiles}

            if gid_str not in self.seen:
                if is_wildcard and bot_ran_before:
                    # Bot was watching "all groups" before, this is a new group
                    # in AdsPower — all its profiles are "missed"
                    self.log(f"🔍 Группа '{name}': новая группа, {len(profiles)} профилей добавлено за время простоя.")
                    self.seen[gid_str] = list(current_ids)
                    if profiles:
                        missed_total += len(profiles)
                        now_str = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
                        tag = tag_by_group.get(name, "").strip()
                        for p in profiles:
                            pname = p.get("name") or p.get("user_id")
                            message, parse_mode = self._build_message(name, pname, tag, now_str)
                            if self._send_with_retry(tg, message, parse_mode):
                                self.log(f"✅ Отправлено (пропущенный): '{pname}' в группе '{name}'")
                else:
                    # First time ever — just remember, don't spam
                    self.seen[gid_str] = list(current_ids)
                    self.log(f"Группа '{name}': начальное состояние — {len(profiles)} профилей.")
            else:
                known_ids = set(self.seen[gid_str])
                new_profiles = [p for p in profiles if str(p.get("user_id")) not in known_ids]
                if new_profiles:
                    missed_total += len(new_profiles)
                    now_str = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
                    tag = tag_by_group.get(name, "").strip()
                    self.log(f"🔍 Группа '{name}': найдено {len(new_profiles)} новых профилей, добавленных за время простоя.")
                    for p in new_profiles:
                        pname = p.get("name") or p.get("user_id")
                        message, parse_mode = self._build_message(name, pname, tag, now_str)
                        if self._send_with_retry(tg, message, parse_mode):
                            self.log(f"✅ Отправлено (пропущенный): '{pname}' в группе '{name}'")
                # Update seen with current state
                self.seen[gid_str] = list(current_ids)

        save_seen(self.seen)
        if missed_total > 0:
            self.log(f"📋 Всего отправлено {missed_total} уведомлений о пропущенных профилях.")
        else:
            self.log("✅ Пропущенных профилей не найдено. Начинаю мониторинг.")

    def _poll_once(self, ads, tg, target_groups, tag_by_group):
        """Single polling iteration across all groups."""
        for name, gid in target_groups.items():
            if self.stop_event.is_set():
                break
            gid_str = str(gid)
            try:
                profiles = ads.list_profiles(gid)
            except Exception as e:
                self.log(f"⚠ Ошибка при опросе группы '{name}': {e}")
                continue

            known_ids = set(self.seen.get(gid_str, []))
            current_ids = set()
            new_profiles = []
            for p in profiles:
                pid = str(p.get("user_id"))
                current_ids.add(pid)
                if pid not in known_ids:
                    new_profiles.append(p)

            if new_profiles:
                now_str = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
                tag = tag_by_group.get(name, "").strip()
                for p in new_profiles:
                    pname = p.get("name") or p.get("user_id")
                    message, parse_mode = self._build_message(name, pname, tag, now_str)
                    if self._send_with_retry(tg, message, parse_mode):
                        self.log(f"✅ Отправлено: '{pname}' в группе '{name}'")

            self.seen[gid_str] = list(current_ids)

        save_seen(self.seen)


# ----------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------
class App:
    def __init__(self, root):
        self.root = root
        root.title("AdsPower -> Telegram Monitor")
        root.geometry("780x680")
        root.minsize(700, 500)

        self.config = load_config()
        self.stop_event = threading.Event()
        self.worker_thread = None

        self._build_ui()
        root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _build_ui(self):
        pad = {"padx": 8, "pady": 3}

        # --- Settings frame (top) ---
        settings_frm = ttk.LabelFrame(self.root, text="Настройки", padding=8)
        settings_frm.pack(fill="x", padx=8, pady=(8, 4))

        labels = [
            "AdsPower API URL:",
            "AdsPower API Key (не обязательно):",
            "Telegram Bot Token:",
            "Telegram Chat ID (группы):",
            "Интервал проверки (сек):",
        ]
        for i, text in enumerate(labels):
            ttk.Label(settings_frm, text=text).grid(row=i, column=0, sticky="w", pady=2)

        self.api_base_var = tk.StringVar(value=self.config["api_base"])
        ttk.Entry(settings_frm, textvariable=self.api_base_var, width=50).grid(row=0, column=1, sticky="we", pady=2)

        self.api_key_var = tk.StringVar(value=self.config.get("api_key", ""))
        ttk.Entry(settings_frm, textvariable=self.api_key_var, width=50).grid(row=1, column=1, sticky="we", pady=2)

        self.tg_token_var = tk.StringVar(value=self.config["telegram_token"])
        ttk.Entry(settings_frm, textvariable=self.tg_token_var, width=50, show="*").grid(row=2, column=1, sticky="we", pady=2)

        self.tg_chat_var = tk.StringVar(value=self.config["telegram_chat_id"])
        ttk.Entry(settings_frm, textvariable=self.tg_chat_var, width=50).grid(row=3, column=1, sticky="we", pady=2)

        self.interval_var = tk.StringVar(value=str(self.config["poll_interval"]))
        ttk.Entry(settings_frm, textvariable=self.interval_var, width=10).grid(row=4, column=1, sticky="w", pady=2)

        settings_frm.columnconfigure(1, weight=1)

        # --- Groups table frame (middle) ---
        groups_frm = ttk.LabelFrame(self.root, text="Группы для мониторинга", padding=8)
        groups_frm.pack(fill="both", expand=True, padx=8, pady=4)

        # Treeview table
        table_container = ttk.Frame(groups_frm)
        table_container.pack(fill="both", expand=True)

        columns = ("name", "tag")
        self.groups_tree = ttk.Treeview(table_container, columns=columns, show="headings", height=6)
        self.groups_tree.heading("name", text="Название группы")
        self.groups_tree.heading("tag", text="Тег в Telegram (@юз / ID)")
        self.groups_tree.column("name", width=300, minwidth=150)
        self.groups_tree.column("tag", width=250, minwidth=100)

        tree_scroll = ttk.Scrollbar(table_container, orient="vertical", command=self.groups_tree.yview)
        self.groups_tree.configure(yscrollcommand=tree_scroll.set)

        self.groups_tree.pack(side="left", fill="both", expand=True)
        tree_scroll.pack(side="right", fill="y")

        # Double-click to edit
        self.groups_tree.bind("<Double-1>", self._on_tree_double_click)

        # Buttons for table management
        table_btn_frame = ttk.Frame(groups_frm)
        table_btn_frame.pack(fill="x", pady=(6, 0))

        ttk.Button(table_btn_frame, text="＋ Добавить", command=self._add_group_row).pack(side="left", padx=2)
        ttk.Button(table_btn_frame, text="🗑 Удалить", command=self._delete_group_row).pack(side="left", padx=2)
        ttk.Button(table_btn_frame, text="✱ Все группы", command=self._add_all_wildcard).pack(side="left", padx=2)

        # Populate table from config
        for g in self.config.get("groups", []):
            self.groups_tree.insert("", "end", values=(g.get("name", ""), g.get("tag", "")))

        # --- Buttons frame ---
        btn_frame = ttk.Frame(self.root)
        btn_frame.pack(fill="x", padx=8, pady=4)

        self.start_btn = ttk.Button(btn_frame, text="▶ Запустить мониторинг", command=self.start)
        self.start_btn.pack(side="left", padx=3)

        self.stop_btn = ttk.Button(btn_frame, text="■ Остановить", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=3)

        self.test_btn = ttk.Button(btn_frame, text="Проверить соединение", command=self.test_connection)
        self.test_btn.pack(side="left", padx=3)

        self.save_btn = ttk.Button(btn_frame, text="Сохранить настройки", command=self.save_settings)
        self.save_btn.pack(side="left", padx=3)

        # --- Log frame (bottom, expandable) ---
        log_frm = ttk.LabelFrame(self.root, text="Журнал", padding=4)
        log_frm.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        self.log_widget = scrolledtext.ScrolledText(log_frm, height=10, state="disabled", wrap="word")
        self.log_widget.pack(fill="both", expand=True)

        self._log_line_count = 0

    # ------------------------------------------------------------------
    # Table management
    # ------------------------------------------------------------------
    def _add_group_row(self):
        """Open a small dialog to add a new group row."""
        dialog = tk.Toplevel(self.root)
        dialog.title("Добавить группу")
        dialog.geometry("420x160")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()

        ttk.Label(dialog, text="Название группы:").grid(row=0, column=0, padx=8, pady=8, sticky="w")
        name_var = tk.StringVar()
        name_entry = ttk.Entry(dialog, textvariable=name_var, width=35)
        name_entry.grid(row=0, column=1, padx=8, pady=8, sticky="we")
        name_entry.focus_set()

        ttk.Label(dialog, text="Тег в Telegram:").grid(row=1, column=0, padx=8, pady=4, sticky="w")
        tag_var = tk.StringVar()
        ttk.Entry(dialog, textvariable=tag_var, width=35).grid(row=1, column=1, padx=8, pady=4, sticky="we")

        ttk.Label(dialog, text="(@username или числовой Telegram ID, можно оставить пустым)", font=("", 9)).grid(
            row=2, column=0, columnspan=2, padx=8, sticky="w"
        )

        def _ok(event=None):
            name = name_var.get().strip()
            if name:
                self.groups_tree.insert("", "end", values=(name, tag_var.get().strip()))
            dialog.destroy()

        name_entry.bind("<Return>", _ok)

        btn_frm = ttk.Frame(dialog)
        btn_frm.grid(row=3, column=0, columnspan=2, pady=10)
        ttk.Button(btn_frm, text="Добавить", command=_ok).pack(side="left", padx=4)
        ttk.Button(btn_frm, text="Отмена", command=dialog.destroy).pack(side="left", padx=4)

        dialog.columnconfigure(1, weight=1)

    def _delete_group_row(self):
        """Delete selected rows from the groups table."""
        selected = self.groups_tree.selection()
        if not selected:
            messagebox.showinfo("Удаление", "Выберите строку в таблице для удаления.")
            return
        for item in selected:
            self.groups_tree.delete(item)

    def _add_all_wildcard(self):
        """Quick-add wildcard * row."""
        # Check if * already exists
        for item in self.groups_tree.get_children():
            vals = self.groups_tree.item(item, "values")
            if vals and vals[0].strip().lower() in ('*', 'все', 'all'):
                messagebox.showinfo("Все группы", "Подстановочный знак (*) уже добавлен.")
                return
        self.groups_tree.insert("", "end", values=("*", ""))

    def _on_tree_double_click(self, event):
        """Edit cell on double-click."""
        item = self.groups_tree.identify_row(event.y)
        column = self.groups_tree.identify_column(event.x)
        if not item or not column:
            return

        col_index = int(column.replace("#", "")) - 1
        current_values = list(self.groups_tree.item(item, "values"))
        current_text = current_values[col_index] if col_index < len(current_values) else ""

        # Get cell bounding box
        bbox = self.groups_tree.bbox(item, column)
        if not bbox:
            return

        # Create edit entry overlay
        entry = ttk.Entry(self.groups_tree)
        entry.insert(0, current_text)
        entry.select_range(0, "end")
        entry.place(x=bbox[0], y=bbox[1], width=bbox[2], height=bbox[3])
        entry.focus_set()

        def _save(event=None):
            new_val = entry.get().strip()
            current_values[col_index] = new_val
            self.groups_tree.item(item, values=current_values)
            entry.destroy()

        def _cancel(event=None):
            entry.destroy()

        entry.bind("<Return>", _save)
        entry.bind("<Escape>", _cancel)
        entry.bind("<FocusOut>", _save)

    # ------------------------------------------------------------------
    # Logging (with line limit for 24/7 operation)
    # ------------------------------------------------------------------
    def log(self, msg):
        def _append():
            self.log_widget.configure(state="normal")
            ts = datetime.now().strftime("%H:%M:%S")
            self.log_widget.insert("end", f"[{ts}] {msg}\n")
            self._log_line_count += 1
            # Trim old lines to prevent memory growth
            if self._log_line_count > MAX_LOG_LINES:
                excess = self._log_line_count - MAX_LOG_LINES
                self.log_widget.delete("1.0", f"{excess + 1}.0")
                self._log_line_count = MAX_LOG_LINES
            self.log_widget.see("end")
            self.log_widget.configure(state="disabled")
        self.root.after(0, _append)

    # ------------------------------------------------------------------
    # Config gathering
    # ------------------------------------------------------------------
    def gather_config(self):
        try:
            interval = int(self.interval_var.get())
        except ValueError:
            interval = 30

        # Read groups from Treeview
        groups = []
        for item in self.groups_tree.get_children():
            vals = self.groups_tree.item(item, "values")
            name = vals[0].strip() if len(vals) > 0 else ""
            tag = vals[1].strip() if len(vals) > 1 else ""
            if name:
                groups.append({"name": name, "tag": tag})

        return {
            "api_base": self.api_base_var.get().strip(),
            "api_key": self.api_key_var.get().strip(),
            "telegram_token": self.tg_token_var.get().strip(),
            "telegram_chat_id": self.tg_chat_var.get().strip(),
            "groups": groups,
            "poll_interval": interval,
        }

    def save_settings(self):
        cfg = self.gather_config()
        save_config(cfg)
        self.log("Настройки сохранены в config.json")

    def test_connection(self):
        cfg = self.gather_config()

        def _run():
            try:
                ads = AdsPowerClient(cfg["api_base"], cfg.get("api_key", ""))
                groups = ads.list_groups()
                names = [g.get("group_name") for g in groups]
                self.log(f"✅ AdsPower подключен. Найдено групп: {len(groups)}. Пример: {names[:5]}")
            except Exception as e:
                self.log(f"❌ Не удалось подключиться к AdsPower: {e}")

            if cfg["telegram_token"] and cfg["telegram_chat_id"]:
                try:
                    tg = TelegramClient(cfg["telegram_token"], cfg["telegram_chat_id"])
                    tg.send_message("✅ Тестовое сообщение от AdsPower Monitor")
                    self.log("✅ Тестовое сообщение отправлено в Telegram")
                except Exception as e:
                    self.log(f"❌ Ошибка Telegram: {e}")
            else:
                self.log("ℹ Токен/Chat ID Telegram не заполнены — тест Telegram пропущен")

        threading.Thread(target=_run, daemon=True).start()

    def start(self):
        cfg = self.gather_config()
        if not cfg["telegram_token"] or not cfg["telegram_chat_id"]:
            messagebox.showwarning("Внимание", "Заполните Telegram Bot Token и Chat ID")
            return
        if not cfg["groups"]:
            messagebox.showwarning("Внимание", "Добавьте хотя бы одну группу в таблицу")
            return

        save_config(cfg)
        self.config = cfg
        self.stop_event = threading.Event()

        monitor = Monitor(cfg, self.log, self.stop_event)
        self.worker_thread = threading.Thread(target=self._safe_run, args=(monitor,), daemon=True)
        self.worker_thread.start()

        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.log("Мониторинг запущен.")

    def _safe_run(self, monitor):
        try:
            monitor.run()
        except Exception:
            self.log("ОШИБКА в фоновом потоке:\n" + traceback.format_exc())
        finally:
            self.root.after(0, self._on_worker_finished)

    def _on_worker_finished(self):
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")

    def stop(self):
        self.stop_event.set()
        self.stop_btn.configure(state="disabled")
        self.log("Останавливаю мониторинг...")

    def on_close(self):
        self.stop_event.set()
        self.save_settings()
        self.root.destroy()


def main():
    root = tk.Tk()
    if platform.system() == 'Darwin':
        root.bind_class("Text", "<Command-c>", lambda e: e.widget.event_generate("<<Copy>>"))
        root.bind_class("Text", "<Command-v>", lambda e: e.widget.event_generate("<<Paste>>"))
        root.bind_class("Text", "<Command-x>", lambda e: e.widget.event_generate("<<Cut>>"))
        root.bind_class("Text", "<Command-a>", lambda e: e.widget.event_generate("<<SelectAll>>"))
        root.bind_class("Entry", "<Command-c>", lambda e: e.widget.event_generate("<<Copy>>"))
        root.bind_class("Entry", "<Command-v>", lambda e: e.widget.event_generate("<<Paste>>"))
        root.bind_class("Entry", "<Command-x>", lambda e: e.widget.event_generate("<<Cut>>"))
        root.bind_class("Entry", "<Command-a>", lambda e: e.widget.event_generate("<<SelectAll>>"))
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()