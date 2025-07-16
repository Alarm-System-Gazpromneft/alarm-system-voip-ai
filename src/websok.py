# websocket_proxy_server.py
import asyncio
import websockets
import json
import socket
import sys
import os
import threading
import time
import queue
import tempfile # Для создания временных файлов
import requests

# --- Импорты для TTS ---
import pyttsx3

# --- Конфигурация сервера ---
WS_HOST = "0.0.0.0"
WS_PORT = 8765

# Конфигурация для SIP-клиента
SIP_CLIENT_HOST = "127.0.0.1"
SIP_CLIENT_COMMAND_PORT = 9999
call_program_path = "/usr/bin/sip-session3"
call_program_cmd = [sys.executable, call_program_path]

# Домен для команды /audio
AUDIO_CALL_DOMAIN = "fekeniyibklof.beget.app"

# Таймауты для SIP-клиента
SIP_CLIENT_RESPONSE_TIMEOUT = 5
PROGRAM_START_DELAY = 5
PROGRAM_GRACEFUL_SHUTDOWN_TIMEOUT = 3
PROGRAM_KILL_TIMEOUT = 1
STATUS_POLL_RETRIES = 3
STATUS_POLL_INTERVAL = 1

# --- Настройки для команды paplay (теперь используется и для TTS) ---
PAPLAY_COMMAND_PATH = "paplay"
TEST_SOUND_FILE = "/app/song.wav"
PAPLAY_DEVICE = "virtual_sorc" # Устройство PulseAudio, куда отправлять звук

# --- Настройки для Vosk распознавания (связь с vosk_recognition_tcp_client.py) ---
VOSK_CLIENT_COMMAND_HOST = "127.0.0.1"
VOSK_CLIENT_COMMAND_PORT = 9990

VOSK_CLIENT_RESULTS_LISTEN_HOST = "127.0.0.1"
VOSK_CLIENT_RESULTS_LISTEN_PORT = 9991

# --- Глобальные состояния ---
current_sip_client_process: asyncio.subprocess.Process = None
websocket_clients: set = set()

# --- Глобальный TTS движок ---
tts_engine: pyttsx3.Engine = None

# --- Глобальные задачи для чтения stdout/stderr SIP-клиента ---
sip_client_stdout_task: asyncio.Task = None
sip_client_stderr_task: asyncio.Task = None

global process228

def generate_tts_audio(
base_url: str,
    token: str,
    ref_audio_path: str,
    ref_text_input: str,
    gen_text_input: str,
    output_audio_path: str,
) -> bool:
    """Генерирует аудио с помощью TTS API. ЭТО СИНХРОННАЯ ФУНКЦИЯ."""
    if not os.path.exists(ref_audio_path):
        print(f"[TTS_API_ERR] Ошибка: Эталонный аудиофайл не найден по пути: {ref_audio_path}", file=sys.stderr)
        return False

    url = f"{base_url}/tts/generate"
    headers = {"Authorization": f"Bearer {token}"}

    data = {
        "ref_text_input": ref_text_input,
        "gen_text_input": gen_text_input,
        "remove_silence": "false",
        "randomize_seed": "true",
        "seed_input": "0",
        "cross_fade_duration_slider": "0.15",
        "nfe_slider": "32",
        "speed_slider": "1.0",
    }

    file_extension = os.path.splitext(ref_audio_path)[1].lower()
    media_type = {
        ".ogg": "audio/ogg",
        ".wav": "audio/wav",
        ".mp3": "audio/mpeg"
    }.get(file_extension, "application/octet-stream")

    try:
        with open(ref_audio_path, "rb") as f:
            files = {"ref_audio_file": (os.path.basename(ref_audio_path), f, media_type)}
            print(f"[TTS_API] Отправка запроса на генерацию TTS для текста: '{gen_text_input[:50]}...'")
            response = requests.post(url, headers=headers, data=data, files=files)
            response.raise_for_status()

            with open(output_audio_path, "wb") as out_f:
                out_f.write(response.content)
            print(f"[TTS_API] Аудио успешно сохранено в: {output_audio_path}")
            return True
    except requests.exceptions.RequestException as e:
        print(f"[TTS_API_ERR] Ошибка при генерации TTS: {e}", file=sys.stderr)
        if hasattr(e, 'response') and e.response is not None:
            print(f"[TTS_API_ERR] Детали ошибки: {e.response.text}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"[TTS_API_ERR] Произошла неожиданная ошибка: {e}", file=sys.stderr)
        return False

async def send_command_to_sip_client(command: str) -> dict:
    """
    Отправляет команду SIP-клиенту по TCP и ждет JSON-ответа.
    """
    reader = None
    writer = None
    response_data_dict = {"status": "error", "message": f"Error: No response from SIP client within {SIP_CLIENT_RESPONSE_TIMEOUT} seconds."}

    try:
        print(f"[SIP_CLIENT_TCP] Попытка соединения с SIP-клиентом на {SIP_CLIENT_HOST}:{SIP_CLIENT_COMMAND_PORT}")
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(SIP_CLIENT_HOST, SIP_CLIENT_COMMAND_PORT),
            timeout=SIP_CLIENT_RESPONSE_TIMEOUT
        )
        print(f"[SIP_CLIENT_TCP] Соединение установлено с SIP-клиентом.")

        encoded_command = (command + '\n').encode('utf-8')
        writer.write(encoded_command)
        await writer.drain()
        print(f"[SIP_CLIENT_TCP] Отправка: '{command}\\n'")

        data = await asyncio.wait_for(reader.readline(), timeout=SIP_CLIENT_RESPONSE_TIMEOUT)
        if data:
            response_str = data.decode('utf-8').strip()
            print(f"[SIP_CLIENT_TCP] Получено: '{response_str}'")
            try:
                response_data_dict = json.loads(response_str)
            except json.JSONDecodeError:
                response_data_dict = {"status": "error", "message": f"Invalid JSON from SIP client: {response_str}"}
        else:
            response_data_dict = {"status": "error", "message": "SIP client closed connection without sending data."}

    except asyncio.TimeoutError:
        response_data_dict = {"status": "error", "message": f"SIP client did not respond within {SIP_CLIENT_RESPONSE_TIMEOUT} seconds."}
        print(f"[SIP_CLIENT_TCP] Ошибка: Таймаут ответа или установления соединения с SIP-клиентом.")
    except ConnectionRefusedError:
        response_data_dict = {"status": "error", "message": "SIP client connection refused (no listener at specified address/port)."}
        print(f"[SIP_CLIENT_TCP] Ошибка: SIP клиент отклонил соединение (не запущен?).")
    except Exception as e:
        response_data_dict = {"status": "error", "message": f"Error communicating with SIP client: {e}"}
        print(f"[SIP_CLIENT_TCP] Общая ошибка при работе с SIP клиентом: {e}")
    finally:
        if writer:
            writer.close()
            await writer.wait_closed()
        print(f"[SIP_CLIENT_TCP] Соединение с SIP клиентом закрыто.")
    return response_data_dict

async def _kill_existing_sip_client_program(force_kill_if_stuck: bool = False):
    """
    Пытается корректно завершить запущенную программу sip-session3, при необходимости убивает.
    """
    global current_sip_client_process, sip_client_stdout_task, sip_client_stderr_task

    if current_sip_client_process is None or current_sip_client_process.returncode is not None:
        if current_sip_client_process:
            print(f"[SIP_PROGRAM] Программа уже завершена (код: {current_sip_client_process.returncode}). Очистка состояния.", file=sys.stderr)
        current_sip_client_process = None
        # Отменяем задачи чтения stdout/stderr, если они были запущены
        if sip_client_stdout_task and not sip_client_stdout_task.done():
            sip_client_stdout_task.cancel()
            try: await sip_client_stdout_task
            except asyncio.CancelledError: pass
        if sip_client_stderr_task and not sip_client_stderr_task.done():
            sip_client_stderr_task.cancel()
            try: await sip_client_stderr_task
            except asyncio.CancelledError: pass
        sip_client_stdout_task = None
        sip_client_stderr_task = None
        return # Нет запущенной программы или уже завершена

    print(f"[SIP_PROGRAM] Попытка завершить существующую программу звонка (PID: {current_sip_client_process.pid}).")

    # 1. Попытка отправить команду 'quit' на TCP-интерфейс sip-session3
    try:
        quit_response = await send_command_to_sip_client("/quit")
        print(f"[SIP_PROGRAM] Ответ SIP-клиента на /quit: {quit_response}", file=sys.stderr)
    except Exception as e:
        print(f"[SIP_PROGRAM] Ошибка при отправке '/quit' SIP-клиенту: {e}", file=sys.stderr)

    # 2. Ожидание корректного завершения процесса
    try:
        await asyncio.wait_for(current_sip_client_process.wait(), timeout=PROGRAM_GRACEFUL_SHUTDOWN_TIMEOUT)
        print("[SIP_PROGRAM] Программа завершилась корректно.")
    except asyncio.TimeoutError:
        print(f"[SIP_PROGRAM] Программа не завершилась корректно за {PROGRAM_GRACEFUL_SHUTDOWN_TIMEOUT} сек. Попытка принудительного завершения.")
        if current_sip_client_process.returncode is None: # Если все еще работает
            current_sip_client_process.terminate() # SIGTERM
            try:
                await asyncio.wait_for(current_sip_client_process.wait(), timeout=PROGRAM_KILL_TIMEOUT)
                print("[SIP_PROGRAM] Программа завершена через terminate().")
            except asyncio.TimeoutError:
                print(f"[SIP_PROGRAM] Программа не завершилась через terminate() за {PROGRAM_KILL_TIMEOUT} сек. Принудительное убийство.")
                current_sip_client_process.kill() # SIGKILL
                await current_sip_client_process.wait() # Ждем завершения
                print("[SIP_PROGRAM] Программа убита.")
    except Exception as e:
        print(f"[SIP_PROGRAM] Неожиданная ошибка при завершении программы: {e}", file=sys.stderr)
    finally:
        current_sip_client_process = None
        # Отменяем задачи чтения stdout/stderr после завершения процесса
        if sip_client_stdout_task and not sip_client_stdout_task.done():
            sip_client_stdout_task.cancel()
            try: await sip_client_stdout_task
            except asyncio.CancelledError: pass
        if sip_client_stderr_task and not sip_client_stderr_task.done():
            sip_client_stderr_task.cancel()
            try: await sip_client_stderr_task
            except asyncio.CancelledError: pass
        sip_client_stdout_task = None
        sip_client_stderr_task = None
        print("[SIP_PROGRAM] Состояние программы очищено.")


async def _play_test_sound():
    """
    Запускает команду paplay для воспроизведения тестового звука.
    Выполняется в фоновом режиме, не блокируя основной цикл.
    """
    paplay_cmd = [PAPLAY_COMMAND_PATH, TEST_SOUND_FILE, f'--device={PAPLAY_DEVICE}']
    print(f"[PAPLAY] Попытка воспроизвести звук: {' '.join(paplay_cmd)}", file=sys.stderr)

    try:
        # Убедимся, что файл существует
        if not os.path.exists(TEST_SOUND_FILE):
            print(f"[PAPLAY_ERR] Ошибка: Тестовый файл звука '{TEST_SOUND_FILE}' не найден.", file=sys.stderr)
            return

        process = await asyncio.create_subprocess_exec(
            *paplay_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        stdout, stderr = await process.communicate()

        if stdout:
            print(f"[PAPLAY_OUT] {stdout.decode().strip()}", file=sys.stderr)
        if stderr:
            print(f"[PAPLAY_ERR] {stderr.decode().strip()}", file=sys.stderr)
        
        if process.returncode == 0:
            print("[PAPLAY] Воспроизведение тестового звука завершено успешно.", file=sys.stderr)
        else:
            print(f"[PAPLAY_ERR] Воспроизведение тестового звука завершилось с ошибкой. Код выхода: {process.returncode}", file=sys.stderr)

    except FileNotFoundError:
        print(f"[PAPLAY_ERR] Ошибка: Команда '{PAPLAY_COMMAND_PATH}' не найдена. Убедитесь, что PulseAudio установлен и 'paplay' находится в PATH.", file=sys.stderr)
    except Exception as e:
        print(f"[PAPLAY_ERR] Произошла ошибка при воспроизведении тестового звука: {e}", file=sys.stderr)

# --- Функции для управления Vosk-клиентом ---

async def send_command_to_vosk_client(command: str) -> dict:
    """
    Отправляет команду на командный TCP-порт Vosk-клиента и ждет JSON-ответа.
    """
    reader = None
    writer = None
    response_data_dict = {"status": "error", "message": f"Error: No response from Vosk client within 3 seconds."} # Уменьшаем таймаут для команд

    try:
        print(f"[VOSK_CMD_TCP] Отправка команды '{command}' на Vosk-клиент {VOSK_CLIENT_COMMAND_HOST}:{VOSK_CLIENT_COMMAND_PORT}")
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(VOSK_CLIENT_COMMAND_HOST, VOSK_CLIENT_COMMAND_PORT),
            timeout=3
        )
        writer.write((command + '\n').encode('utf-8'))
        await writer.drain()

        data = await asyncio.wait_for(reader.readline(), timeout=3)
        if data:
            response_str = data.decode('utf-8').strip()
            print(f"[VOSK_CMD_TCP] Получено от Vosk-клиента: '{response_str}'")
            try:
                response_data_dict = json.loads(response_str)
            except json.JSONDecodeError:
                response_data_dict = {"status": "error", "message": f"Invalid JSON from Vosk client: {response_str}"}
        else:
            response_data_dict = {"status": "error", "message": "Vosk client closed connection without sending data."}

    except asyncio.TimeoutError:
        response_data_dict = {"status": "error", "message": f"Vosk client did not respond to command '{command}' within 3 seconds."}
        print(f"[VOSK_CMD_TCP] Ошибка: Таймаут ответа от Vosk-клиента.")
    except ConnectionRefusedError:
        response_data_dict = {"status": "error", "message": "Vosk client connection refused. Is vosk_recognition_tcp_client.py running?"}
        print(f"[VOSK_CMD_TCP] Ошибка: Vosk-клиент отклонил соединение. Убедитесь, что 'vosk_recognition_tcp_client.py' запущен и слушает на {VOSK_CLIENT_COMMAND_PORT}.")
    except Exception as e:
        response_data_dict = {"status": "error", "message": f"Error communicating with Vosk client for command '{command}': {e}"}
        print(f"[VOSK_CMD_TCP] Общая ошибка при связи с Vosk-клиентом: {e}")
    finally:
        if writer:
            writer.close()
            await writer.wait_closed()
    return response_data_dict

async def _handle_vosk_results_from_client(reader, writer):
    """
    Обработчик для входящих TCP-соединений от vosk_recognition_tcp_client.py
    (получение результатов распознавания).
    """
    addr = writer.get_extra_info('peername')
    print(f"[VOSK_RESULTS_TCP] Получено соединение от Vosk-клиента {addr}", file=sys.stderr)
    try:
        while True:
            data = await reader.readline()
            if not data:
                print(f"[VOSK_RESULTS_TCP] Соединение с Vosk-клиентом {addr} закрыто.", file=sys.stderr)
                break
            
            result_str = data.decode('utf-8').strip()
            
            try:
                result_json = json.loads(result_str)
                # --- РАССЫЛКА НА WS-КЛИЕНТЫ ---
                disconnected_clients = set()
                for client_ws in list(websocket_clients):
                    try:
                        await client_ws.send(json.dumps(result_json))
                    except websockets.exceptions.ConnectionClosedOK:
                        disconnected_clients.add(client_ws)
                    except websockets.exceptions.ConnectionClosedError as e:
                        print(f"[WS_BROADCAST_ERR] Ошибка отправки на {client_ws.remote_address}: {e}", file=sys.stderr)
                        disconnected_clients.add(client_ws)
                    except Exception as e:
                        print(f"[WS_BROADCAST_ERR] Неожиданная ошибка отправки на {client_ws.remote_address}: {e}", file=sys.stderr)
                        disconnected_clients.add(client_ws)
                websocket_clients.difference_update(disconnected_clients)

            except json.JSONDecodeError:
                print(f"[VOSK_RESULTS_TCP_ERR] Невалидный JSON от Vosk-клиента: '{result_str}'", file=sys.stderr)
            except Exception as e:
                print(f"[VOSK_RESULTS_TCP_ERR] Ошибка при обработке результата от Vosk-клиента: {e}", file=sys.stderr)

    except asyncio.CancelledError:
        print(f"[VOSK_RESULTS_TCP] Обработка результатов от {addr} отменена.", file=sys.stderr)
    except Exception as e:
        print(f"[VOSK_RESULTS_TCP_ERR] Общая ошибка в обработчике Vosk-результатов: {e}", file=sys.stderr)
    finally:
        writer.close()
        await writer.wait_closed()

# --- Новая функция для генерации голоса по тексту ---
async def _generate_and_play_speech(text: str) -> dict:
    """
    Генерирует речь из текста, сохраняет в WAV и воспроизводит с помощью paplay.
    Выполняет блокирующие операции pyttsx3 и subprocess.
    """
    global tts_engine

    if tts_engine is None:
        print("[TTS_ERR] TTS движок не инициализирован. Невозможно воспроизвести речь.", file=sys.stderr)
        return {"status": "error", "message": "TTS engine not initialized. Check server logs for initialization errors."}

    print(f"[TTS] Попытка сгенерировать и воспроизвести: '{text}'", file=sys.stderr)
    
    # Создаем временный WAV-файл
    temp_wav_file = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_f:
            temp_wav_file = tmp_f.name
        
        # Генерация речи и сохранение в WAV-файл
        # pyttsx3.save_to_file является блокирующей, но управляет собственным потоком/вызовом
        # engine.runAndWait() будет блокировать, поэтому используем asyncio.to_thread
        # await asyncio.to_thread(lambda: tts_engine.save_to_file(text, temp_wav_file))
        # await asyncio.to_thread(tts_engine.runAndWait) # Ждем завершения сохранения
        b = await asyncio.to_thread(generate_tts_audio,
            base_url="http://cert.bvksite.com:8208",
            token="bbbbdjdosjfwdjiopy7878r6oejdsdfl2djkldsjklfsiojfw",
            ref_audio_path="/app/base.mp3",
            ref_text_input="Сегодня утром лёгкий ветер шуршал листьями под окнами, создавая атмосферу спокойствия в саду. The soft wind rustled the leaves outside the window this morning, creating a serene atmosphere in the garden.",
            gen_text_input=text,
            output_audio_path=temp_wav_file,)
        if not b:
            print("[TTS_API_ERR] Генерация речи через API не удалась.", file=sys.stderr)
            return {"status": "error", "message": "Failed to generate speech via API."}

        print(f"[TTS] Речь сохранена во временный файл: {temp_wav_file}", file=sys.stderr)

        # Воспроизведение через paplay
        paplay_cmd = [PAPLAY_COMMAND_PATH, temp_wav_file, f'--device={PAPLAY_DEVICE}']
        print(f"[PAPLAY] Воспроизведение TTS: {' '.join(paplay_cmd)}", file=sys.stderr)
        global process228
        process228 = await asyncio.create_subprocess_exec(
            *paplay_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process228.communicate()

        if stdout:
            print(f"[PAPLAY_OUT] {stdout.decode().strip()}", file=sys.stderr)
        if stderr:
            print(f"[PAPLAY_ERR] {stderr.decode().strip()}", file=sys.stderr)

        if process228.returncode == 0:
            print("[TTS] Воспроизведение TTS завершено успешно.", file=sys.stderr)
            return {"status": "success", "message": "Speech generated, saved and played."}
        else:
            print(f"[TTS_ERR] Воспроизведение TTS завершилось с ошибкой. Код выхода: {process228.returncode}", file=sys.stderr)
            return {"status": "error", "message": f"Failed to play TTS audio via paplay: {process228.returncode}"}

    except Exception as e:
        print(f"[TTS_ERR] Общая ошибка при генерации/воспроизведении TTS: {e}", file=sys.stderr)
        return {"status": "error", "message": f"Failed to generate or play speech: {e}"}
    finally:
        if temp_wav_file and os.path.exists(temp_wav_file):
            try:
                os.remove(temp_wav_file) # Удаляем временный файл
                print(f"[TTS] Временный файл {temp_wav_file} удален.", file=sys.stderr)
            except Exception as e:
                print(f"[TTS_ERR] Не удалось удалить временный TTS файл {temp_wav_file}: {e}", file=sys.stderr)


# --- Функция для чтения stdout SIP-клиента и отправки DTMF ---
async def _read_sip_client_stdout_and_handle_dtmf():
    global current_sip_client_process, websocket_clients
    if current_sip_client_process is None or current_sip_client_process.stdout is None:
        print("[SIP_PROGRAM_READER] SIP-клиент не запущен или нет stdout для чтения.", file=sys.stderr)
        return

    reader = current_sip_client_process.stdout
    print("[SIP_PROGRAM_READER] Запущена задача чтения stdout SIP-клиента для DTMF.", file=sys.stderr)
    
    # Регулярное выражение для поиска "Got DMTF X"
    import re
    dtmf_pattern = re.compile(r"Got DMTF (\S+)")

    try:
        while True:
            line = await reader.readline()
            if not line: # EOF - процесс завершился
                print("[SIP_PROGRAM_READER] stdout SIP-клиента закрыт (EOF).", file=sys.stderr)
                break
            
            decoded_line = line.decode('utf-8', errors='ignore').strip()
            print(f"[PROGRAM_OUT] {decoded_line}", file=sys.stderr) # Печатаем весь stdout
            
            # Проверяем на DTMF
            match = dtmf_pattern.search(decoded_line)
            if match:
                dtmf_digit = match.group(1)
                print(f"[DTMF_DETECTED] Обнаружен DTMF: {dtmf_digit}. Отправка по WebSocket.", file=sys.stderr)
                
                dtmf_event = {"event": "dtmf_received", "digit": dtmf_digit}
                
                disconnected_clients = set()
                for client_ws in list(websocket_clients):
                    try:
                        await client_ws.send(json.dumps(dtmf_event))
                    except websockets.exceptions.ConnectionClosedOK:
                        disconnected_clients.add(client_ws)
                    except websockets.exceptions.ConnectionClosedError as e:
                        print(f"[WS_BROADCAST_ERR] Ошибка отправки DTMF на {client_ws.remote_address}: {e}", file=sys.stderr)
                        disconnected_clients.add(client_ws)
                    except Exception as e:
                        print(f"[WS_BROADCAST_ERR] Неожиданная ошибка отправки DTMF на {client_ws.remote_address}: {e}", file=sys.stderr)
                        disconnected_clients.add(client_ws)
                websocket_clients.difference_update(disconnected_clients)

    except asyncio.CancelledError:
        print("[SIP_PROGRAM_READER] Задача чтения stdout SIP-клиента отменена.", file=sys.stderr)
    except Exception as e:
        print(f"[SIP_PROGRAM_READER_ERR] Ошибка при чтении stdout SIP-клиента: {e}", file=sys.stderr)


async def websocket_handler(websocket, path):
    """
    Обработчик для входящих WebSocket-соединений.
    """
    global current_sip_client_process, websocket_clients # Добавляем sip_client_stdout_task в global
    # Добавляем sip_client_stdout_task и sip_client_stderr_task в global здесь,
    # так как они могут быть присвоены внутри этой функции (при запуске sip-клиента).
    global sip_client_stdout_task, sip_client_stderr_task


    client_address = websocket.remote_address
    print(f"[WS] Новое WebSocket-соединение от {client_address}")
    websocket_clients.add(websocket) # Добавляем нового клиента в список

    try:
        async for message in websocket:
            print(f"[WS] Получено сообщение от {client_address}: {message}")
            ws_response = {} # Ответ для WebSocket клиента

            try:
                request = json.loads(message)
                command = request.get("command")

                if command == "status":
                    # Запрос статуса у SIP-клиента
                    sip_client_response = await send_command_to_sip_client("/status")
                    ws_response = {
                        "status": "success",
                        "command": "status",
                        "sip_client_response": sip_client_response
                    }
                elif command == "call":
                    number = request.get("number")
                    if number is None:
                        ws_response = {"status": "error", "message": "Missing 'number' for 'call' command."}
                        await websocket.send(json.dumps(ws_response))
                        continue

                    # Проверка статуса SIP-клиента перед звонком
                    call_active_on_sip_client = False
                    sip_client_status_response = {}
                    for i in range(STATUS_POLL_RETRIES):
                        sip_client_status_response = await send_command_to_sip_client("/status")
                        if sip_client_status_response.get("status") == "active":
                            call_active_on_sip_client = True
                            print(f"[WS] SIP-клиент сообщил о статусе 'active' после {i+1} попыток.")
                            break
                        print(f"[WS] SIP-клиент статус не 'active' ({sip_client_status_response.get('status')}). Попытка {i+1}/{STATUS_POLL_RETRIES}. Ожидание {STATUS_POLL_INTERVAL} сек...")
                        await asyncio.sleep(STATUS_POLL_INTERVAL)
                    
                    if call_active_on_sip_client:
                        if current_sip_client_process and current_sip_client_process.returncode is None:
                            ws_response = {
                                "status": "error",
                                "message": "Call already active, please hangup first.",
                                "sip_client_status": sip_client_status_response
                            }
                            await websocket.send(json.dumps(ws_response))
                            continue
                        else:
                            print("[WS] SIP-клиент активен, но локальной программы нет или она завершилась. Попытка сброса состояния SIP-клиента и переинициализации.")
                            await send_command_to_sip_client("/hangup")
                            await asyncio.sleep(0.1)


                    if current_sip_client_process and current_sip_client_process.returncode is None:
                        print("[WS] Обнаружен незавершенный процесс SIP-клиента. Принудительное завершение перед новым звонком.")
                        await _kill_existing_sip_client_program(force_kill_if_stuck=True)
                        await asyncio.sleep(0.5)

                    # Запуск внешней программы SIP-клиента
                    print(f"[WS] Запуск внешней программы SIP-клиента '{call_program_path}'...", file=sys.stderr)
                    try:
                        current_sip_client_process = await asyncio.create_subprocess_exec(
                            *call_program_cmd,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE
                        )
                        # Создаем задачи для чтения stdout и stderr, теперь stdout будет парситься
                        sip_client_stdout_task = asyncio.create_task(_read_sip_client_stdout_and_handle_dtmf())
                        sip_client_stderr_task = asyncio.create_task(current_sip_client_process.stderr.read())
                        sip_client_stderr_task.add_done_callback(
                            lambda f: print(f"[PROGRAM_ERR] {f.result().decode('utf-8').strip()}", file=sys.stderr) if f.result() else None
                        )

                        print(f"[WS] Программа SIP-клиента '{call_program_path}' запущена с PID {current_sip_client_process.pid}. Ожидание {PROGRAM_START_DELAY} сек...", file=sys.stderr)
                        await asyncio.sleep(PROGRAM_START_DELAY)

                        # Отправка команды "audio" на SIP-клиент (через его TCP-интерфейс)
                        call_command_for_sip = f"/audio {number}@{AUDIO_CALL_DOMAIN}"
                        sip_client_response_call = await send_command_to_sip_client(call_command_for_sip)
                        
                        ws_response = {
                            "status": "success",
                            "command": "call",
                            "number": number,
                            "sip_client_response": sip_client_response_call,
                            "program_pid": current_sip_client_process.pid
                        }
                    except FileNotFoundError:
                        ws_response = {"status": "error", "message": f"Program '{call_program_path}' not found. Make sure it's in the correct path."}
                        print(f"[WS] Ошибка: Программа '{call_program_path}' не найдена.", file=sys.stderr)
                    except Exception as e:
                        ws_response = {"status": "error", "message": f"Failed to start/communicate with SIP client: {e}"}
                        print(f"[WS] Ошибка при запуске/связи с программой SIP-клиента: {e}", file=sys.stderr)
                        await _kill_existing_sip_client_program()
                
                elif command == "hangup":
                    # Отправка команды "hangup" на SIP-клиент (через его TCP-интерфейс)
                    sip_client_response = await send_command_to_sip_client("/hangup")
                    ws_response = {
                        "status": "success",
                        "command": "hangup",
                        "sip_client_response": sip_client_response
                    }
                
                elif command == "quit":
                    print("[WS] Получена команда 'quit'. Принудительное завершение программы SIP-клиента.", file=sys.stderr)
                    await _kill_existing_sip_client_program(force_kill_if_stuck=True)
                    ws_response = {
                        "status": "success",
                        "command": "quit",
                        "message": "SIP client program terminated."
                    }
                
                elif command == "test_sound":
                    asyncio.create_task(_play_test_sound())
                    ws_response = {
                        "status": "success",
                        "command": "test_sound",
                        "message": "Test sound playback initiated."
                    }

                elif command == "start_recognition":
                    # Отправляем команду Vosk-клиенту по TCP
                    response = await send_command_to_vosk_client("start_recognition")
                    ws_response = {
                        "status": response["status"],
                        "command": "start_recognition",
                        "message": response["message"]
                    }
                elif command == "stop_recognition":
                    # Отправляем команду Vosk-клиенту по TCP
                    response = await send_command_to_vosk_client("stop_recognition")
                    ws_response = {
                        "status": response["status"],
                        "command": "stop_recognition",
                        "message": response["message"]
                    }

                elif command == "speak":
                    text_to_speak = request.get("text")
                    if text_to_speak:
                        # Запускаем генерацию и воспроизведение речи в фоновом режиме
                        # Результат _generate_and_play_speech можно было бы логировать
                        global process228
                        try:
                            process228.kill()
                        except:
                            pass
                        asyncio.create_task(_generate_and_play_speech(text_to_speak))
                        ws_response = {
                            "status": "success",
                            "command": "speak",
                            "message": "Speech generation initiated."
                        }
                    else:
                        ws_response = {
                            "status": "error",
                            "message": "Missing 'text' for 'speak' command."
                        }
                
                else: # Любые другие команды перенаправляются на SIP-клиент
                    sip_client_response = await send_command_to_sip_client(f"/{command}")
                    ws_response = {
                        "status": "success",
                        "command": command,
                        "sip_client_response": sip_client_response
                    }

            except json.JSONDecodeError:
                ws_response = {
                    "status": "error",
                    "message": "Invalid JSON format."
                }
            except Exception as e:
                ws_response = {
                    "status": "error",
                    "message": f"Server processing error: {e}"
                }

            await websocket.send(json.dumps(ws_response))
            print(f"[WS] Отправлен ответ на {client_address}: {json.dumps(ws_response)}")

    except websockets.exceptions.ConnectionClosedOK:
        print(f"[WS] Соединение закрыто {client_address} (нормально).", file=sys.stderr)
    except websockets.exceptions.ConnectionClosedError as e:
        print(f"[WS] Соединение закрыто с ошибкой {client_address}: {e}", file=sys.stderr)
    except Exception as e:
        print(f"[WS] Неожиданная ошибка в обработчике WebSocket: {e}", file=sys.stderr)
    finally:
        if websocket in websocket_clients: # Проверяем, чтобы избежать KeyError
            websocket_clients.remove(websocket)


async def main():
    global tts_engine # Объявляем глобальную переменную tts_engine

    print(f"Запуск WebSocket сервера на ws://{WS_HOST}:{WS_PORT}")
    print(f"Сервер будет общаться с SIP-клиентом на {SIP_CLIENT_HOST}:{SIP_CLIENT_COMMAND_PORT}")
    print(f"Сервер будет запускать внешний SIP-клиент: {call_program_cmd}")
    print(f"Сервер будет общаться с Vosk-клиентом (для команд) на {VOSK_CLIENT_COMMAND_HOST}:{VOSK_CLIENT_COMMAND_PORT}")
    print(f"Сервер будет слушать результаты Vosk-клиента на {VOSK_CLIENT_RESULTS_LISTEN_HOST}:{VOSK_CLIENT_RESULTS_LISTEN_PORT}")

    # Инициализация TTS движка
    print("[TTS] Инициализация TTS движка...", file=sys.stderr)
    try:
        tts_engine = pyttsx3.init()
        # Опционально: настройка свойств голоса (скорость, громкость, выбор голоса)
        # voices = tts_engine.getProperty('voices')
        # for voice in voices:
        #     print(f"TTS Voice: ID={voice.id}, Name={voice.name}, Lang={voice.languages}", file=sys.stderr)
        # tts_engine.setProperty('voice', voices[0].id) # Выбрать первый доступный голос
        tts_engine.setProperty('rate', 180) # Скорость речи (слова в минуту)
        tts_engine.setProperty('volume', 1.0) # Громкость (0.0 до 1.0)
        print("[TTS] TTS движок инициализирован.", file=sys.stderr)
    except Exception as e:
        print(f"[TTS_ERR] Ошибка инициализации TTS движка: {e}. Функционал TTS будет недоступен.", file=sys.stderr)
        tts_engine = None # Устанавливаем в None, если инициализация не удалась

    # Убедиться, что процесс SIP-клиента очищен при старте
    await _kill_existing_sip_client_program(force_kill_if_stuck=True)

    # Запустить TCP-сервер для приема результатов от Vosk-клиента
    vosk_results_server = await asyncio.start_server(
        _handle_vosk_results_from_client, VOSK_CLIENT_RESULTS_LISTEN_HOST, VOSK_CLIENT_RESULTS_LISTEN_PORT
    )
    results_addr = vosk_results_server.sockets[0].getsockname()
    print(f"[VOSK_RESULTS_TCP] Сервер для результатов Vosk запущен на {results_addr}", file=sys.stderr)


    async with websockets.serve(websocket_handler, WS_HOST, WS_PORT):
        await vosk_results_server.serve_forever() # Запускаем сервер для результатов Vosk на неопределенное время

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[SERVER] WebSocket сервер остановлен вручную.", file=sys.stderr)
    except Exception as e:
        print(f"[SERVER] Критическая ошибка при работе сервера: {e}", file=sys.stderr)
    finally:
        # Функция для асинхронной очистки TTS движка
        async def _cleanup_tts_async():
            if tts_engine:
                try:
                    await asyncio.to_thread(tts_engine.stop) 
                    print("[TTS] TTS движок остановлен.", file=sys.stderr)
                except Exception as e:
                    print(f"[TTS_ERR] Ошибка при остановке TTS движка: {e}", file=sys.stderr)

        # Вызываем асинхронную функцию очистки TTS в новом цикле событий, 
        # только если основной цикл уже завершился или его нет.
        try:
            asyncio.run(_cleanup_tts_async())
        except RuntimeError as e:
            print(f"[TTS_ERR] Не удалось корректно остановить TTS движок (RuntimeError: {e}).", file=sys.stderr)
        except Exception as e:
            print(f"[TTS_ERR] Неожиданная ошибка при очистке TTS: {e}", file=sys.stderr)


        print("[SERVER] Выполняется очистка запущенных процессов SIP-клиента...", file=sys.stderr)
        asyncio.run(_kill_existing_sip_client_program(force_kill_if_stuck=True))
        print("[SERVER] Очистка завершена. Сервер остановлен.", file=sys.stderr)