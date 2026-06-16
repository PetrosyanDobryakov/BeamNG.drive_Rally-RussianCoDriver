import socket
import json
import random
import winsound
import wave
import struct
import tempfile
import os
import re
import time
import threading
from collections import deque
import tkinter as tk
from tkinter import ttk, scrolledtext, filedialog, messagebox
import webbrowser

# ------------------------------------------------------------
# Глобальные переменные
# ------------------------------------------------------------
UDP_IP = '127.0.0.1'
UDP_PORT = 12347
MAPPING_FILE = "component_mapping.json"
SYSTEM_MAPPING_FILE = "system_mapping.json"
SYSTEM_DELAY_FILE = "system_delay.json"
CONFIG_FILE = "config.json"

component_map = {}
system_map = {}
system_delays = {}
current_volume = 1.0
running = False
root = None

# Путь к игре и другие настройки из config.json
game_path = ""
first_run_done = False

# --- ИСПРАВЛЕНО: инициализируем виджеты, которые создадутся в main ---
debug_text = None
start_btn = None
stop_btn = None

# ------------------------------------------------------------
# Загрузка/сохранение конфига
# ------------------------------------------------------------
def load_config():
    global game_path, first_run_done
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
        game_path = cfg.get("game_path", "")
        first_run_done = cfg.get("first_run_done", False)
        return cfg
    return {}

def save_config(cfg):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=2)

# ------------------------------------------------------------
# Поиск игры
# ------------------------------------------------------------
def find_beamng_standard():
    possible = [
        r"C:\Program Files (x86)\Steam\steamapps\common\BeamNG.drive\BeamNG.drive.exe",
        r"C:\Program Files\Steam\steamapps\common\BeamNG.drive\BeamNG.drive.exe",
    ]
    for p in possible:
        if os.path.exists(p):
            return p
    import glob
    candidates = glob.glob(r"C:\*\Steam\steamapps\common\BeamNG.drive\BeamNG.drive.exe")
    if candidates:
        return candidates[0]
    return None

def is_game_path_valid():
    return os.path.exists(game_path)

# ------------------------------------------------------------
# Запрос прав администратора и патч Lua
# ------------------------------------------------------------
def patch_lua():
    if not game_path or not os.path.exists(game_path):
        log("Ошибка: путь к игре не найден.")
        return False
    lua_path = os.path.join(os.path.dirname(game_path), "lua", "ge", "extensions", "gameplay", "rally", "audioManager.lua")
    if not os.path.exists(lua_path):
        log(f"Ошибка: audioManager.lua не найден по пути {lua_path}")
        return False

    backup_path = "backup_audioManager.lua"
    if not os.path.exists(backup_path):
        import shutil
        shutil.copy2(lua_path, backup_path)
        log("Создана резервная копия оригинального audioManager.lua.")

    with open(lua_path, 'r', encoding='utf-8') as f:
        content = f.read()

    if "udp:setpeername" in content:
        log("Патч уже применён.")
        return True

    content = content.replace(
        "local rallyUtil = require('/lua/ge/extensions/gameplay/rally/util')",
        "local rallyUtil = require('/lua/ge/extensions/gameplay/rally/util')\n\n"
        "local socket = require('socket')\n"
        "local udp = socket.udp()\n"
        "udp:setpeername('127.0.0.1', 12347)"
    )

    old_enqueue = "audioObjs = pacenote:audioObjs()"
    new_enqueue = (
        "  if pacenote.notes and pacenote.notes.english and pacenote.notes.english.note then\n"
        "    local structured = pacenote.notes.english.note.structured\n"
        "    if structured then\n"
        "      udp:send('PHRASE:' .. table.concat(structured, '|'))\n"
        "    end\n"
        "  end\n"
        "  " + old_enqueue
    )
    content = content.replace(old_enqueue, new_enqueue, 1)

    old_system = "log('D', logTag, string.format(\"RallyMode: playing system pacenote: '%s'\", pacenote.text))"
    new_system = (
        "if pacenote.text then\n"
        "    udp:send('SYSTEM:' .. pacenote.text)\n"
        "  end\n"
        "  " + old_system
    )
    content = content.replace(old_system, new_system, 1)

    with open(lua_path, 'w', encoding='utf-8') as f:
        f.write(content)

    log("audioManager.lua успешно пропатчен.")
    return True

# Ковыряем старые пути

def update_audio_paths_in_mapping(mapping):
    """Заменяет абсолютные пути на пути относительно папки audio рядом с .exe."""
    exe_dir = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(__file__)
    audio_dir = os.path.join(exe_dir, "audio")
    if not os.path.exists(audio_dir):
        return  # если папки audio нет, ничего не меняем
    for key, file_list in mapping.items():
        new_list = []
        for old_path in file_list:
            # Извлекаем только имя файла из старого пути
            fname = os.path.basename(old_path)
            new_path = os.path.join(audio_dir, fname).replace('\\', '/')
            new_list.append(new_path)
        mapping[key] = new_list

# ------------------------------------------------------------
# Загрузка словарей
# ------------------------------------------------------------
def load_mappings():
    global component_map, system_map, system_delays
    try:
        with open(MAPPING_FILE, 'r', encoding='utf-8') as f:
            component_map = json.load(f)
        update_audio_paths_in_mapping(component_map)   # ← обновляем пути
    except Exception as e:
        log(f"Ошибка загрузки component_mapping.json: {e}")

    system_map = {}
    if os.path.exists(SYSTEM_MAPPING_FILE):
        with open(SYSTEM_MAPPING_FILE, 'r', encoding='utf-8') as f:
            system_map = json.load(f)
        update_audio_paths_in_mapping(system_map)      # ← обновляем пути

    system_delays = {}
    if os.path.exists(SYSTEM_DELAY_FILE):
        with open(SYSTEM_DELAY_FILE, 'r', encoding='utf-8') as f:
            system_delays = json.load(f)

# ------------------------------------------------------------
# Аудио функции (с поддержкой громкости)
# ------------------------------------------------------------
def clean_word(w):
    return re.sub(r'^[?!.,;:]+|[?!.,;:]+$', '', w.strip()).lower()

def normalize_numbers(text):
    return re.sub(r'\b(\d+)\s+(?=\d)', r'\1', text)

def split_phrase(phrase, mapping):
    phrase = normalize_numbers(phrase)
    words = [clean_word(w) for w in phrase.split()]
    result = []
    i = 0
    while i < len(words):
        best_len = 0
        best_key = None
        for key in mapping.keys():
            key_words = key.split()
            if words[i:i+len(key_words)] == key_words:
                if len(key_words) > best_len:
                    best_len = len(key_words)
                    best_key = key
        if best_key:
            result.append(best_key)
            i += best_len
        else:
            result.append(words[i])
            i += 1
    return result

def wav_duration(path):
    try:
        with wave.open(path, 'rb') as wf:
            return wf.getnframes() / wf.getframerate() if wf.getframerate() > 0 else 0.5
    except Exception:
        return 0.5

def convert_to_mono(stereo_path, volume=1.0):
    with wave.open(stereo_path, 'rb') as wav_in:
        params = wav_in.getparams()
        if params.nchannels != 2:
            frames = wav_in.readframes(params.nframes)
            samples = struct.unpack('<' + 'h' * (len(frames) // 2), frames)
            adjusted = [int(min(max(s * volume, -32768), 32767)) for s in samples]
            mono_frames = struct.pack('<' + 'h' * len(adjusted), *adjusted)
            tmp = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
            with wave.open(tmp.name, 'wb') as wav_out:
                wav_out.setparams((params.nchannels, params.sampwidth, params.framerate, params.nframes,
                                   params.comptype, params.compname))
                wav_out.writeframes(mono_frames)
            return tmp.name
        frames = wav_in.readframes(params.nframes)
        samples = struct.unpack('<' + 'h' * (len(frames) // 2), frames)
        mono = [(samples[i] + samples[i+1]) // 2 for i in range(0, len(samples), 2)]
        adjusted = [int(min(max(s * volume, -32768), 32767)) for s in mono]
        mono_frames = struct.pack('<' + 'h' * len(adjusted), *adjusted)
        tmp = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
        with wave.open(tmp.name, 'wb') as wav_out:
            wav_out.setparams((1, params.sampwidth, params.framerate, len(adjusted),
                               params.comptype, params.compname))
            wav_out.writeframes(mono_frames)
        return tmp.name

def play_sequence(components):
    for comp in components:
        files = component_map.get(comp)
        if files:
            chosen = random.choice(files)
            log(f"  ▶ {comp} -> {os.path.basename(chosen)}")
            mono_path = convert_to_mono(chosen, current_volume)
            try:
                duration = wav_duration(mono_path)
                winsound.PlaySound(mono_path, winsound.SND_ASYNC | winsound.SND_NODEFAULT)
                time.sleep(duration + 0.02)
            except Exception as e:
                log(f"  ⚠ Ошибка воспроизведения {comp}: {e}")
            finally:
                if mono_path != chosen:
                    try:
                        os.unlink(mono_path)
                    except:
                        pass
        else:
            log(f"  ❌ Нет файла для: {comp}")

def play_system(phrase_text):
    text = phrase_text.strip().lower()
    delay = system_delays.get(text, 0.0)
    if delay > 0:
        time.sleep(delay)
    files = system_map.get(text)
    if files:
        chosen = random.choice(files)
        log(f"  ▶ [SYSTEM] {text} -> {os.path.basename(chosen)}")
        mono_path = convert_to_mono(chosen, current_volume)
        try:
            duration = wav_duration(mono_path)
            winsound.PlaySound(mono_path, winsound.SND_ASYNC | winsound.SND_NODEFAULT)
            time.sleep(duration + 0.02)
        except Exception as e:
            log(f"  ⚠ Ошибка воспроизведения системного: {e}")
        finally:
            if mono_path != chosen:
                try:
                    os.unlink(mono_path)
                except:
                    pass
    else:
        log(f"  ❌ Нет файла для системного сообщения: {text}")

# ------------------------------------------------------------
# Очередь и потоки
# ------------------------------------------------------------
phrase_queue = deque()
queue_lock = threading.Lock()

def player_thread_func():
    while running:
        with queue_lock:
            if phrase_queue:
                item = phrase_queue.popleft()
            else:
                item = None
        if item:
            if item[0] == 'phrase':
                play_sequence(item[1])
            elif item[0] == 'system':
                play_system(item[1])
        else:
            time.sleep(0.05)

def udp_listener():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((UDP_IP, UDP_PORT))
    sock.settimeout(0.5)
    last_raw_msg = ""
    last_raw_time = 0

    while running:
        try:
            data, addr = sock.recvfrom(4096)
            msg = data.decode().strip()

            if msg.startswith('PHRASE:'):
                now = time.time()
                if msg == last_raw_msg and (now - last_raw_time) < 0.4:
                    continue
                last_raw_msg = msg
                last_raw_time = now

                phrase = msg[7:]
                raw_parts = phrase.split('|')
                all_components = []
                for part in raw_parts:
                    all_components.extend(split_phrase(part, component_map))

                log(f"Фраза: {all_components}")
                with queue_lock:
                    phrase_queue.append(('phrase', all_components))

            elif msg.startswith('SYSTEM:'):
                text = msg[7:]
                log(f"Системное сообщение: {text}")
                with queue_lock:
                    phrase_queue.append(('system', text))

        except socket.timeout:
            continue
        except Exception as e:
            if running:
                log(f"Ошибка UDP: {e}")
    sock.close()

# ------------------------------------------------------------
# GUI и логирование
# ------------------------------------------------------------
def log(message):
    if root and debug_text:   # теперь debug_text глобальна, не вызовет ошибку
        debug_text.insert(tk.END, message + "\n")
        debug_text.see(tk.END)
    print(message)

def start_engine():
    global running, udp_thread, player_thread
    if running:
        return
    if not is_game_path_valid():
        messagebox.showerror("Ошибка", "Не найден BeamNG.drive.exe. Проверьте путь в настройках.")
        return
    running = True
    load_mappings()
    log("Словари загружены.")
    udp_thread = threading.Thread(target=udp_listener, daemon=True)
    udp_thread.start()
    player_thread = threading.Thread(target=player_thread_func, daemon=True)
    player_thread.start()
    log("Русский штурман активен.")
    start_btn.config(state=tk.DISABLED)
    stop_btn.config(state=tk.NORMAL)

def stop_engine():
    global running
    running = False
    log("Штурман остановлен.")
    start_btn.config(state=tk.NORMAL)
    stop_btn.config(state=tk.DISABLED)

def set_volume(val):
    global current_volume
    current_volume = float(val) / 100.0

def open_settings():
    settings_win = tk.Toplevel(root)
    settings_win.title("Настройки")
    settings_win.geometry("500x300")
    settings_win.resizable(False, False)

    main_frame = tk.Frame(settings_win)
    main_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

    link_frame = tk.Frame(main_frame)
    link_frame.pack(fill=tk.X, pady=5)

    def open_version():
        # СЮДА ВСТАВИТЬ ВАШУ ССЫЛКУ НА ПРОВЕРКУ ВЕРСИИ
        webbrowser.open("https://example.com/version")

    def open_report():
        # СЮДА ВСТАВИТЬ ССЫЛКУ ДЛЯ СООБЩЕНИЯ ОБ ОШИБКЕ
        webbrowser.open("https://example.com/report")

    tk.Label(link_frame, text="Проверить версию", fg="blue", cursor="hand2", font=("Arial", 10, "underline")).pack(side=tk.LEFT, padx=5)
    tk.Label(link_frame, text="Сообщить об ошибке", fg="blue", cursor="hand2", font=("Arial", 10, "underline")).pack(side=tk.LEFT, padx=5)
    for label in link_frame.winfo_children():
        if label.cget("text") == "Проверить версию":
            label.bind("<Button-1>", lambda e: open_version())
        elif label.cget("text") == "Сообщить об ошибке":
            label.bind("<Button-1>", lambda e: open_report())

    path_frame = tk.Frame(main_frame)
    path_frame.pack(fill=tk.X, pady=15)
    tk.Label(path_frame, text="Директория игры:", font=("Arial", 10)).pack(side=tk.LEFT)
    path_var = tk.StringVar(value=game_path if game_path else "не задан")
    tk.Label(path_frame, textvariable=path_var, font=("Arial", 9, "italic")).pack(side=tk.LEFT, padx=5)

    info_frame = tk.Frame(main_frame)
    info_frame.pack(side=tk.BOTTOM, fill=tk.X, pady=5)

    authors = [
        "Создано: Петросян Довряков",
        "Сделал озвучку: Сергей День",
        "Озвучивал: Илья Баландин"
    ]
    for text in authors:
        tk.Label(info_frame, text=text, font=("Arial", 9)).pack(anchor=tk.E)

def on_closing():
    stop_engine()
    root.destroy()

import sys

def ensure_audio_folder():
    exe_dir = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(__file__)
    audio_dir = os.path.join(exe_dir, "audio")
    if not os.path.exists(audio_dir):
        # Если запущено из PyInstaller, можно попытаться извлечь из _MEIPASS
        if getattr(sys, 'frozen', False):
            src = os.path.join(sys._MEIPASS, "audio")
            if os.path.exists(src):
                import shutil
                shutil.copytree(src, audio_dir)
                log("Аудиофайлы извлечены во внешнюю папку.")
                return
        # Иначе просто предупреждаем
        log("⚠ Папка audio не найдена. Поместите папку audio рядом с программой.")

# ------------------------------------------------------------
# Первый запуск и инициализация
# ------------------------------------------------------------
def first_run_setup():
    log("Выполняется первый запуск...")
    messagebox.showinfo("Внимание", "Программа на стадии разработки. Разработчик не претендует на авторство аудиозаписей.")
    found = find_beamng_standard()
    if not found:
        messagebox.showinfo("Поиск игры", "BeamNG.drive не найден в стандартных папках Steam. Укажите путь к BeamNG.drive.exe вручную.")
        found = filedialog.askopenfilename(title="Укажите BeamNG.drive.exe", filetypes=[("BeamNG.drive.exe", "BeamNG.drive.exe")])
    if found:
        global game_path
        game_path = found
        save_config({"game_path": game_path, "first_run_done": False})
    else:
        messagebox.showerror("Ошибка", "Путь к игре не указан. Программа не может продолжить.")
        return False

    if not patch_lua():
        return False

    messagebox.showinfo("Первый запуск завершён", "Настройка завершена. В программе есть вкладка «Настройки» с дополнительными функциями.")

    cfg = load_config()
    cfg["first_run_done"] = True
    save_config(cfg)
    global first_run_done
    first_run_done = True
    log("Первый запуск успешно завершён.")
    return True

import sys


# Прооверка аудио


def ensure_audio_folder():
    exe_dir = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(__file__)
    audio_dir = os.path.join(exe_dir, "audio")
    if not os.path.exists(audio_dir):
        # Если запущено из PyInstaller, можно попытаться извлечь из _MEIPASS
        if getattr(sys, 'frozen', False):
            src = os.path.join(sys._MEIPASS, "audio")
            if os.path.exists(src):
                import shutil
                shutil.copytree(src, audio_dir)
                log("Аудиофайлы извлечены во внешнюю папку.")
                return
        # Иначе просто предупреждаем
        log("⚠ Папка audio не найдена. Поместите папку audio рядом с программой.")
# ------------------------------------------------------------
# Запуск GUI
# ------------------------------------------------------------

def main():
    global root, start_btn, stop_btn, debug_text
    # Авто-проверка аудио
    ensure_audio_folder()

    root = tk.Tk()
    root.title("Русский штурман BeamNG.drive")
    root.geometry("700x450")
    root.protocol("WM_DELETE_WINDOW", on_closing)

    # Загружаем конфиг
    load_config()
    if not first_run_done:
        if not first_run_setup():
            root.destroy()
            return
    else:
        if not is_game_path_valid():
            messagebox.showwarning("Внимание", "BeamNG.drive.exe не найден по сохранённому пути. Будет запущена процедура первого запуска.")
            if not first_run_setup():
                root.destroy()
                return

    # Создаём виджеты после возможного первого запуска
    top_frame = tk.Frame(root)
    top_frame.pack(fill=tk.X, padx=10, pady=5)
    tk.Label(top_frame, text="Русский штурман BeamNG.drive", font=("Arial", 14, "bold")).pack(side=tk.LEFT, padx=5)
    settings_btn = tk.Button(top_frame, text="⚙", font=("Arial", 12), command=open_settings)
    settings_btn.pack(side=tk.RIGHT, padx=5)

    control_frame = tk.Frame(root)
    control_frame.pack(fill=tk.X, padx=10, pady=5)

    start_btn = tk.Button(control_frame, text="Старт", width=10, command=start_engine)
    start_btn.pack(side=tk.LEFT, padx=5)
    stop_btn = tk.Button(control_frame, text="Стоп", width=10, command=stop_engine, state=tk.DISABLED)
    stop_btn.pack(side=tk.LEFT, padx=5)

    volume_label = tk.Label(control_frame, text="Громкость:")
    volume_label.pack(side=tk.LEFT, padx=(20, 5))
    volume_slider = ttk.Scale(control_frame, from_=0, to=100, orient=tk.HORIZONTAL, command=set_volume)
    volume_slider.set(100)
    volume_slider.pack(side=tk.LEFT, padx=5)

    debug_frame = ttk.LabelFrame(root, text="Отладка")
    debug_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
    debug_text = scrolledtext.ScrolledText(debug_frame, wrap=tk.WORD, height=10)
    debug_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

    root.mainloop()

if __name__ == "__main__":
    main()