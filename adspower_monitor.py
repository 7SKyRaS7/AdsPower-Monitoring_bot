#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AdsPower Profile Monitor -> Telegram Notifier
================================================

Что делает программа:
- Подключается к локальному API AdsPower (по умолчанию http://local.adspower.net:50325)
- Периодически проверяет указанные вами группы профилей
- Как только в группе появляется новый профиль — отправляет уведомление в Telegram

Формат сообщения в Telegram:
    Группа: <название группы>
    Добавлен новый профиль: <название профиля>
    Время добавления: <время>

Требования:
    pip install requests

Запуск:
    python adspower_monitor.py

Все настройки (адрес API, ключ, список групп, токен бота, chat_id, интервал)
вводятся прямо в открывшемся окне и сохраняются в файл config.json рядом
со скриптом, так что при следующем запуске вводить их заново не нужно.
"""

import json
import os
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

DEFAULT_CONFIG = {
    "api_base": "http://local.adspower.net:50325",
    "api_key": "",              # нужен только для облачного (Global) API AdsPower, для локального обычно не требуется
    "telegram_token": "",
    "telegram_chat_id": "",
    "groups": "",               # названия групп, по одному на строку
    "poll_interval": 30,        # секунды
}


def parse_groups_field(text):
    """
    Парсит поле "Группы" из UI. Формат каждой строки:

        Название группы | @username_ответственного

    Часть после "|" необязательна — если её нет, уведомления по этой
    группе будут идти без тега. В качестве тега можно указать:
      - @username  -> подставится как обычное текстовое упоминание
      - числовой Telegram ID -> подставится как кликабельное упоминание
        (полезно для людей без публичного username)

    Возвращает список кортежей (group_name, tag).
    """
    result = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if "|" in line:
            name, tag = line.split("|", 1)
            result.append((name.strip(), tag.strip()))
        else:
            result.append((line, ""))
    return result


# ----------------------------------------------------------------------
# AdsPower API client
# ----------------------------------------------------------------------
class AdsPowerClient:
    # AdsPower Local API ограничивает частоту запросов (~1 запрос/сек).
    # Держим минимальный интервал между запросами, чтобы не ловить
    # "Too many request per second" ошибку.
    MIN_REQUEST_INTERVAL = 1.1  # секунд

    def __init__(self, api_base, api_key=""):
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key.strip()
        self._last_request_time = 0.0

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

        # Пробуем несколько раз, если всё же поймали ограничение по частоте
        last_error = None
        for attempt in range(3):
            self._throttle()
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
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
        """Возвращает список всех групп: [{'group_id':.., 'group_name':..}, ...]"""
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
        """Возвращает список профилей в группе: [{'user_id':.., 'name':..}, ...]"""
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
# ДЕМО-клиент AdsPower (для проверки бота без реального API/ключа)
# ----------------------------------------------------------------------
class FakeAdsPowerClient:
    """
    Имитирует AdsPower API: у неё уже есть несколько "групп" с несколькими
    "профилями", и каждые несколько опросов она сама добавляет один новый
    профиль в случайную группу. Это позволяет проверить всю цепочку
    (обнаружение нового профиля -> сообщение в Telegram) без реального
    подключения к AdsPower и без API-ключа.
    """
    def __init__(self, api_base="demo", api_key=""):
        import random
        self._random = random
        self._groups = [
            {"group_id": "1", "group_name": "Facebook"},
            {"group_id": "2", "group_name": "TikTok"},
            {"group_id": "3", "group_name": "Google Ads"},
        ]
        self._profiles = {
            "1": [{"user_id": "f1", "name": "FB Profile 1"}, {"user_id": "f2", "name": "FB Profile 2"}],
            "2": [{"user_id": "t1", "name": "TikTok Profile 1"}],
            "3": [{"user_id": "g1", "name": "Google Profile 1"}],
        }
        self._counter = 0
        self._tick = 0

    def list_groups(self):
        return list(self._groups)

    def list_profiles(self, group_id):
        gid = str(group_id)
        self._tick += 1
        # Каждые 3 опроса — добавляем новый тестовый профиль в случайную группу
        if self._tick % 3 == 0:
            self._counter += 1
            target_gid = self._random.choice(list(self._profiles.keys()))
            new_id = f"demo-{self._counter}"
            new_name = f"Тестовый профиль #{self._counter}"
            self._profiles[target_gid].append({"user_id": new_id, "name": new_name})
        return list(self._profiles.get(gid, []))


# ----------------------------------------------------------------------
# Telegram client
# ----------------------------------------------------------------------
class TelegramClient:
    def __init__(self, token, chat_id):
        self.token = token.strip()
        self.chat_id = chat_id.strip()

    def send_message(self, text, parse_mode=None):
        if not self.token or not self.chat_id:
            raise RuntimeError("Не заполнены токен бота или chat_id Telegram")
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        data = {"chat_id": self.chat_id, "text": text}
        if parse_mode:
            data["parse_mode"] = parse_mode
        resp = requests.post(url, data=data, timeout=15)
        resp.raise_for_status()
        result = resp.json()
        if not result.get("ok"):
            raise RuntimeError(f"Telegram API error: {result}")
        return result


# ----------------------------------------------------------------------
# Persistent "seen profiles" storage (чтобы после перезапуска не слать
# повторные уведомления по уже известным профилям)
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
    with open(SEEN_PATH, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=2)


# ----------------------------------------------------------------------
# Config load/save
# ----------------------------------------------------------------------
def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            merged = dict(DEFAULT_CONFIG)
            merged.update(cfg)
            return merged
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# ----------------------------------------------------------------------
# Monitor worker (runs in background thread)
# ----------------------------------------------------------------------
class Monitor:
    def __init__(self, config, log_fn, stop_event):
        self.config = config
        self.log = log_fn
        self.stop_event = stop_event
        self.seen = load_seen()  # {group_id_str: [profile_id, ...]}

    def run(self):
        if self.config.get("demo_mode"):
            self.log("⚠ ДЕМО-РЕЖИМ включён: используются тестовые данные, реальный AdsPower не опрашивается.")
            ads = FakeAdsPowerClient()
        else:
            ads = AdsPowerClient(self.config["api_base"], self.config.get("api_key", ""))
        tg = TelegramClient(self.config["telegram_token"], self.config["telegram_chat_id"])

        parsed_groups = parse_groups_field(self.config["groups"])
        if self.config.get("demo_mode") and not parsed_groups:
            # В демо-режиме подставляем тестовые названия групп, если поле пустое
            parsed_groups = [("Facebook", "@example_tag"), ("TikTok", ""), ("Google Ads", "")]
            self.log("Демо-режим: поле групп пустое, использую тестовые группы Facebook / TikTok / Google Ads")
        if not parsed_groups:
            self.log("Не указано ни одной группы. Добавьте названия групп и запустите заново.")
            return

        wanted_names = [name for name, _tag in parsed_groups]
        tag_by_group = {name: tag for name, tag in parsed_groups}

        self.log("Получаю список групп из AdsPower...")
        try:
            all_groups = ads.list_groups()
        except Exception as e:
            self.log(f"ОШИБКА при получении списка групп: {e}")
            return

        name_to_id = {g.get("group_name"): g.get("group_id") for g in all_groups}

        target_groups = {}
        for wanted in wanted_names:
            if wanted in name_to_id:
                target_groups[wanted] = name_to_id[wanted]
            else:
                self.log(f"⚠ Группа '{wanted}' не найдена в AdsPower — проверьте название.")

        if not target_groups:
            self.log("Ни одна из указанных групп не найдена. Остановка.")
            return

        self.log(f"Слежу за группами: {', '.join(target_groups.keys())}")
        self.log(f"Интервал проверки: {self.config['poll_interval']} сек.")

        # Первый проход: если для группы ещё нет истории — просто запоминаем
        # текущие профили как "уже известные", чтобы не спамить при первом запуске.
        for name, gid in target_groups.items():
            gid_str = str(gid)
            if gid_str not in self.seen:
                try:
                    profiles = ads.list_profiles(gid)
                    self.seen[gid_str] = [str(p.get("user_id")) for p in profiles]
                    self.log(f"Группа '{name}': начальное состояние — {len(profiles)} профилей.")
                except Exception as e:
                    self.log(f"ОШИБКА при получении профилей группы '{name}': {e}")
        save_seen(self.seen)

        # Основной цикл
        while not self.stop_event.is_set():
            for name, gid in target_groups.items():
                if self.stop_event.is_set():
                    break
                gid_str = str(gid)
                try:
                    profiles = ads.list_profiles(gid)
                except Exception as e:
                    self.log(f"ОШИБКА при опросе группы '{name}': {e}")
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
                        base_text = (
                            f"Группа: {name}\n"
                            f"Добавлен новый профиль: {pname}\n"
                            f"Время добавления: {now_str}"
                        )
                        parse_mode = None
                        if tag:
                            if tag.startswith("@"):
                                # обычное текстовое упоминание по username
                                message = base_text + f"\nОтветственный: {tag}"
                            elif tag.lstrip("-").isdigit():
                                # упоминание по числовому Telegram ID (для людей без username)
                                message = base_text + f'\nОтветственный: <a href="tg://user?id={tag}">тег</a>'
                                parse_mode = "HTML"
                            else:
                                message = base_text + f"\nОтветственный: {tag}"
                        else:
                            message = base_text
                        try:
                            tg.send_message(message, parse_mode=parse_mode)
                            self.log(f"✅ Отправлено уведомление: '{pname}' в группе '{name}'")
                        except Exception as e:
                            self.log(f"ОШИБКА отправки в Telegram: {e}")

                self.seen[gid_str] = list(current_ids)

            save_seen(self.seen)

            # Ждём интервал, но проверяем stop_event каждую секунду, чтобы
            # остановка происходила быстро.
            waited = 0
            interval = max(5, int(self.config.get("poll_interval", 30)))
            while waited < interval and not self.stop_event.is_set():
                time.sleep(1)
                waited += 1

        self.log("Мониторинг остановлен.")


# ----------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------
class App:
    def __init__(self, root):
        self.root = root
        root.title("AdsPower -> Telegram Monitor")
        root.geometry("640x640")

        self.config = load_config()
        self.stop_event = threading.Event()
        self.worker_thread = None

        pad = {"padx": 8, "pady": 4}

        frm = ttk.Frame(root)
        frm.pack(fill="x", **pad)

        ttk.Label(frm, text="AdsPower API URL:").grid(row=0, column=0, sticky="w")
        self.api_base_var = tk.StringVar(value=self.config["api_base"])
        ttk.Entry(frm, textvariable=self.api_base_var, width=45).grid(row=0, column=1, sticky="we")

        ttk.Label(frm, text="AdsPower API Key (не обязательно для локального API):").grid(row=1, column=0, sticky="w")
        self.api_key_var = tk.StringVar(value=self.config.get("api_key", ""))
        ttk.Entry(frm, textvariable=self.api_key_var, width=45).grid(row=1, column=1, sticky="we")

        ttk.Label(frm, text="Telegram Bot Token:").grid(row=2, column=0, sticky="w")
        self.tg_token_var = tk.StringVar(value=self.config["telegram_token"])
        ttk.Entry(frm, textvariable=self.tg_token_var, width=45, show="*").grid(row=2, column=1, sticky="we")

        ttk.Label(frm, text="Telegram Chat ID (группы):").grid(row=3, column=0, sticky="w")
        self.tg_chat_var = tk.StringVar(value=self.config["telegram_chat_id"])
        ttk.Entry(frm, textvariable=self.tg_chat_var, width=45).grid(row=3, column=1, sticky="we")

        ttk.Label(frm, text="Интервал проверки (сек):").grid(row=4, column=0, sticky="w")
        self.interval_var = tk.StringVar(value=str(self.config["poll_interval"]))
        ttk.Entry(frm, textvariable=self.interval_var, width=10).grid(row=4, column=1, sticky="w")

        self.demo_var = tk.BooleanVar(value=self.config.get("demo_mode", False))
        ttk.Checkbutton(
            frm,
            text="Демо-режим (тестовые данные, без реального AdsPower и без API-ключа)",
            variable=self.demo_var,
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(4, 0))

        frm.columnconfigure(1, weight=1)

        ttk.Label(
            root,
            text=(
                "Названия групп AdsPower (по одному на строку).\n"
                "Чтобы тегать ответственного за группу, добавьте после \"|\" его @username "
                "или числовой Telegram ID:\n"
                "   Facebook | @ivan_manager\n"
                "   TikTok | 123456789\n"
                "   Google Ads   (без тега — можно не указывать)"
            ),
            justify="left",
        ).pack(anchor="w", padx=8)
        self.groups_text = tk.Text(root, height=7)
        self.groups_text.pack(fill="x", padx=8, pady=4)
        self.groups_text.insert("1.0", self.config.get("groups", ""))

        btn_frame = ttk.Frame(root)
        btn_frame.pack(fill="x", padx=8, pady=6)

        self.start_btn = ttk.Button(btn_frame, text="▶ Запустить мониторинг", command=self.start)
        self.start_btn.pack(side="left", padx=4)

        self.stop_btn = ttk.Button(btn_frame, text="■ Остановить", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=4)

        self.test_btn = ttk.Button(btn_frame, text="Проверить соединение", command=self.test_connection)
        self.test_btn.pack(side="left", padx=4)

        self.save_btn = ttk.Button(btn_frame, text="Сохранить настройки", command=self.save_settings)
        self.save_btn.pack(side="left", padx=4)

        ttk.Label(root, text="Журнал:").pack(anchor="w", padx=8)
        self.log_widget = scrolledtext.ScrolledText(root, height=16, state="disabled")
        self.log_widget.pack(fill="both", expand=True, padx=8, pady=4)

        root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ------------------------------------------------------------------
    def log(self, msg):
        def _append():
            self.log_widget.configure(state="normal")
            ts = datetime.now().strftime("%H:%M:%S")
            self.log_widget.insert("end", f"[{ts}] {msg}\n")
            self.log_widget.see("end")
            self.log_widget.configure(state="disabled")
        self.root.after(0, _append)

    def gather_config(self):
        try:
            interval = int(self.interval_var.get())
        except ValueError:
            interval = 30
        return {
            "api_base": self.api_base_var.get().strip(),
            "api_key": self.api_key_var.get().strip(),
            "telegram_token": self.tg_token_var.get().strip(),
            "telegram_chat_id": self.tg_chat_var.get().strip(),
            "groups": self.groups_text.get("1.0", "end").strip(),
            "poll_interval": interval,
            "demo_mode": bool(self.demo_var.get()),
        }

    def save_settings(self):
        cfg = self.gather_config()
        save_config(cfg)
        self.log("Настройки сохранены в config.json")

    def test_connection(self):
        cfg = self.gather_config()

        def _run():
            if cfg.get("demo_mode"):
                self.log("ℹ Демо-режим включён — проверка реального AdsPower пропущена, использую тестовые данные.")
            else:
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
        if not cfg["groups"] and not cfg.get("demo_mode"):
            messagebox.showwarning("Внимание", "Укажите хотя бы одну группу AdsPower")
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
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()