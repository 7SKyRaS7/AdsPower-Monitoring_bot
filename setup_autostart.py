import os
import sys
import platform

def get_startup_folder():
    if platform.system() != "Windows":
        return None
    return os.path.join(
        os.environ.get("APPDATA", ""), 
        "Microsoft", "Windows", "Start Menu", "Programs", "Startup"
    )

def install():
    if platform.system() != "Windows":
        print("Ошибка: Этот скрипт предназначен только для Windows.")
        return

    startup_folder = get_startup_folder()
    if not startup_folder or not os.path.exists(startup_folder):
        print("Ошибка: Не удалось найти папку автозагрузки Windows.")
        return

    bot_dir = os.path.dirname(os.path.abspath(__file__))
    bat_path = os.path.join(startup_folder, "AdsPowerMonitor.bat")
    
    # Create a batch file that runs the bot in headless mode without keeping cmd window open
    # We use pythonw.exe if available, otherwise python.exe
    python_exe = sys.executable
    if python_exe.endswith("python.exe"):
        pythonw_exe = python_exe.replace("python.exe", "pythonw.exe")
        if os.path.exists(pythonw_exe):
            python_exe = pythonw_exe

    bat_content = f"""@echo off
cd /d "{bot_dir}"
start "" "{python_exe}" adspower_monitor.py --headless
"""

    with open(bat_path, "w", encoding="utf-8") as f:
        f.write(bat_content)

    print(f"✅ Успешно добавлено в автозагрузку Windows: {bat_path}")
    print("Бот (в фоновом режиме) будет запускаться автоматически при включении компьютера.")

def remove():
    if platform.system() != "Windows":
        print("Ошибка: Этот скрипт предназначен только для Windows.")
        return

    startup_folder = get_startup_folder()
    if not startup_folder:
        return

    bat_path = os.path.join(startup_folder, "AdsPowerMonitor.bat")
    if os.path.exists(bat_path):
        os.remove(bat_path)
        print("✅ Бот успешно удален из автозагрузки.")
    else:
        print("ℹ Бот не был найден в автозагрузке.")

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("install", "remove"):
        print("Использование: python setup_autostart.py [install | remove]")
        sys.exit(1)

    if sys.argv[1] == "install":
        install()
    elif sys.argv[1] == "remove":
        remove()
