# websocket_proxy_server.py
import asyncio
import websockets
import json
import socket # Для использования ConnectionRefusedError
import sys
import os

# --- Конфигурация сервера ---
WS_HOST = "0.0.0.0" # Хост для WebSocket сервера
WS_PORT = 8765        # Порт для WebSocket сервера

# Конфигурация для TCP Backend Server (для проверки статуса)
TCP_BACKEND_HOST = "127.0.0.1"
TCP_BACKEND_PORT = 9999

# Полный путь к исполняемой программе SIP-сессии
# В реальной системе: /usr/bin/sip-session3
# Для тестирования с mock-версией: 'python sip-session3_mock.py' или полный путь к нему
# Примечание: 'sys.executable' гарантирует, что используется тот же интерпретатор Python
# Если sip-session3 - это не Python скрипт, а бинарник, то просто укажите его путь:
# call_program_path = "/usr/bin/sip-session3"
# call_program_cmd = [call_program_path]
call_program_path = "/usr/bin/sip-session3" # Имя файла mock-программы
call_program_cmd = [sys.executable, call_program_path] # Команда для запуска mock-программы

# Домен для команды /audio, как указано в примере
AUDIO_CALL_DOMAIN = "fekeniyibklof.beget.app"

# Таймаут для ожидания ответа от TCP Backend Server (в секундах)
TCP_RESPONSE_TIMEOUT = 5

# Таймауты и задержки для управления внешней программой
PROGRAM_START_DELAY = 10 # Задержка после запуска программы перед отправкой команды
PROGRAM_GRACEFUL_SHUTDOWN_TIMEOUT = 3 # Таймаут для "мягкого" завершения (после "quit" на stdin)
PROGRAM_KILL_TIMEOUT = 1 # Таймаут для "жесткого" завершения (после terminate, перед kill)
STATUS_POLL_RETRIES = 3 # Количество попыток проверки статуса TCP Backend
STATUS_POLL_INTERVAL = 1 # Интервал между попытками проверки статуса

# --- Глобальное состояние для управления внешней программой ---
# current_call_program_process хранит объект Popen для запущенной внешней программы
current_call_program_process: asyncio.subprocess.Process = None
current_call_program_stdin_writer: asyncio.StreamWriter = None
current_call_program_stdout_reader: asyncio.StreamReader = None
program_stdout_task: asyncio.Task = None
program_stderr_task: asyncio.Task = None


async def send_to_tcp_backend(command: str) -> dict:
    """
    Отправляет команду TCP Backend Server и ждет JSON-ответа.
    """
    reader = None
    writer = None
    response_data_dict = {"status": "error", "message": f"Error: No response from TCP backend server within {TCP_RESPONSE_TIMEOUT} seconds."}

    try:
        print(f"[TCP_BACKEND] Попытка соединения с {TCP_BACKEND_HOST}:{TCP_BACKEND_PORT}")
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(TCP_BACKEND_HOST, TCP_BACKEND_PORT),
            timeout=TCP_RESPONSE_TIMEOUT
        )
        print(f"[TCP_BACKEND] Соединение установлено с {TCP_BACKEND_HOST}:{TCP_BACKEND_PORT}")

        encoded_command = (command + '\n').encode('utf-8')
        writer.write(encoded_command)
        await writer.drain()
        print(f"[TCP_BACKEND] Отправка: '{command}\\n'")

        data = await asyncio.wait_for(reader.readline(), timeout=TCP_RESPONSE_TIMEOUT)
        if data:
            response_str = data.decode('utf-8').strip()
            print(f"[TCP_BACKEND] Получено: '{response_str}'")
            try:
                response_data_dict = json.loads(response_str)
            except json.JSONDecodeError:
                response_data_dict = {"status": "error", "message": f"Invalid JSON from backend: {response_str}"}
        else:
            response_data_dict = {"status": "error", "message": "TCP backend server closed connection without sending data."}

    except asyncio.TimeoutError:
        response_data_dict = {"status": "error", "message": f"TCP backend server did not respond within {TCP_RESPONSE_TIMEOUT} seconds."}
        print(f"[TCP_BACKEND] Ошибка: Таймаут ответа или установления соединения с TCP backend сервером.")
    except ConnectionRefusedError:
        response_data_dict = {"status": "error", "message": "TCP backend server connection refused (no listener at specified address/port)."}
        print(f"[TCP_BACKEND] Ошибка: TCP backend сервер отклонил соединение (не запущен?).")
    except Exception as e:
        response_data_dict = {"status": "error", "message": f"Error communicating with TCP backend server: {e}"}
        print(f"[TCP_BACKEND] Общая ошибка при работе с TCP backend: {e}")
    finally:
        if writer:
            writer.close()
            await writer.wait_closed()
        print(f"[TCP_BACKEND] Соединение с TCP backend сервером закрыто.")
    return response_data_dict


async def _send_command_to_call_program(command: str):
    """Отправляет команду на stdin запущенной программы."""
    global current_call_program_stdin_writer
    if current_call_program_stdin_writer and not current_call_program_stdin_writer.is_closing():
        try:
            print(f"[PROGRAM] Отправка команды '{command}' на stdin программы.")
            current_call_program_stdin_writer.write((command + '\n').encode('utf-8'))
            await current_call_program_stdin_writer.drain()
        except Exception as e:
            print(f"[PROGRAM] Ошибка при отправке на stdin программы: {e}", file=sys.stderr)
    else:
        print("[PROGRAM] Попытка отправить команду, но программа не запущена или stdin не доступен.", file=sys.stderr)


async def _read_output_from_call_program():
    """Читает stdout запущенной программы и печатает."""
    global current_call_program_stdout_reader
    if not current_call_program_stdout_reader:
        return

    while True:
        try:
            line = await current_call_program_stdout_reader.readline()
            if not line: # EOF - программа завершилась
                print("[PROGRAM] stdout программы закрыт (EOF).", file=sys.stderr)
                break
            decoded_line = line.decode('utf-8').strip()
            print(f"[PROGRAM_OUT] {decoded_line}")
            # Здесь можно добавить логику парсинга JSON из stdout программы, если это нужно
            # try:
            #     program_output_json = json.loads(decoded_line)
            #     print(f"[PROGRAM_OUT_JSON] {program_output_json}")
            # except json.JSONDecodeError:
            #     pass # Не JSON, просто печатаем как строку
        except asyncio.CancelledError:
            print("[PROGRAM] Задача чтения stdout отменена.", file=sys.stderr)
            break
        except Exception as e:
            print(f"[PROGRAM] Ошибка при чтении stdout программы: {e}", file=sys.stderr)
            break


async def _kill_existing_call_program(force_kill_if_stuck: bool = False):
    """
    Пытается корректно завершить запущенную программу, при необходимости убивает.
    """
    global current_call_program_process, current_call_program_stdin_writer, \
           current_call_program_stdout_reader, program_stdout_task, program_stderr_task

    if current_call_program_process is None or current_call_program_process.returncode is not None:
        # Программы нет или она уже завершилась
        if current_call_program_process:
            print(f"[PROGRAM] Программа уже завершена (код: {current_call_program_process.returncode}). Очистка состояния.")
        current_call_program_process = None
        current_call_program_stdin_writer = None
        current_call_program_stdout_reader = None
        # Отменить задачи чтения, если они еще не завершены
        if program_stdout_task and not program_stdout_task.done():
            program_stdout_task.cancel()
            try: await program_stdout_task
            except asyncio.CancelledError: pass
        if program_stderr_task and not program_stderr_task.done():
            program_stderr_task.cancel()
            try: await program_stderr_task
            except asyncio.CancelledError: pass
        program_stdout_task = None
        program_stderr_task = None
        return # Нет запущенной программы или уже завершена

    print(f"[PROGRAM] Попытка завершить существующую программу звонка (PID: {current_call_program_process.pid}).")

    # 1. Попытка отправить команду 'quit'
    if current_call_program_stdin_writer:
        try:
            await _send_command_to_call_program("quit")
            # Закрываем writer, чтобы программа получила EOF, если она так обрабатывает завершение
            current_call_program_stdin_writer.close()
            await current_call_program_stdin_writer.wait_closed()
            print("[PROGRAM] Stdin writer закрыт.")
        except Exception as e:
            print(f"[PROGRAM] Ошибка при отправке 'quit' и закрытии stdin: {e}", file=sys.stderr)

    # 2. Ожидание корректного завершения
    try:
        await asyncio.wait_for(current_call_program_process.wait(), timeout=PROGRAM_GRACEFUL_SHUTDOWN_TIMEOUT)
        print("[PROGRAM] Программа завершилась корректно.")
    except asyncio.TimeoutError:
        print(f"[PROGRAM] Программа не завершилась корректно за {PROGRAM_GRACEFUL_SHUTDOWN_TIMEOUT} сек. Попытка принудительного завершения.")
        if current_call_program_process.returncode is None: # Если все еще работает
            current_call_program_process.terminate() # SIGTERM
            try:
                await asyncio.wait_for(current_call_program_process.wait(), timeout=PROGRAM_KILL_TIMEOUT)
                print("[PROGRAM] Программа завершена через terminate().")
            except asyncio.TimeoutError:
                print(f"[PROGRAM] Программа не завершилась через terminate() за {PROGRAM_KILL_TIMEOUT} сек. Принудительное убийство.")
                current_call_program_process.kill() # SIGKILL
                await current_call_program_process.wait() # Ждем завершения
                print("[PROGRAM] Программа убита.")
    except Exception as e:
        print(f"[PROGRAM] Неожиданная ошибка при завершении программы: {e}", file=sys.stderr)
    finally:
        # Отменяем задачи чтения stdout/stderr, если они еще активны
        if program_stdout_task and not program_stdout_task.done():
            program_stdout_task.cancel()
            try: await program_stdout_task
            except asyncio.CancelledError: pass
        if program_stderr_task and not program_stderr_task.done():
            program_stderr_task.cancel()
            try: await program_stderr_task
            except asyncio.CancelledError: pass

        current_call_program_process = None
        current_call_program_stdin_writer = None
        current_call_program_stdout_reader = None
        program_stdout_task = None
        program_stderr_task = None
        print("[PROGRAM] Состояние программы очищено.")


async def websocket_handler(websocket, path):
    """
    Обработчик для входящих WebSocket-соединений.
    """
    # >>>>>> ЭТИ СТРОКИ ПЕРЕМЕЩЕНЫ В САМОЕ НАЧАЛО ФУНКЦИИ <<<<<<
    global current_call_program_process, current_call_program_stdin_writer, \
           current_call_program_stdout_reader, program_stdout_task, program_stderr_task
    # >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>

    client_address = websocket.remote_address
    print(f"[WS] Новое WebSocket-соединение от {client_address}")

    try:
        async for message in websocket:
            print(f"[WS] Получено сообщение от {client_address}: {message}")
            ws_response = {} # Ответ для WebSocket клиента

            try:
                request = json.loads(message)
                command = request.get("command")
                await send_to_tcp_backend("/output")
                if command == "status":
                    backend_response = await send_to_tcp_backend("/status")
                    ws_response = {
                        "status": "success",
                        "command": "status",
                        "backend_response": backend_response
                    }
                elif command == "call":
                    number = request.get("number")
                    if number is None:
                        ws_response = {"status": "error", "message": "Missing 'number' for 'call' command."}
                        await websocket.send(json.dumps(ws_response))
                        continue

                    # 1. Проверка текущего статуса через TCP Backend Server
                    call_active_on_backend = False
                    status_backend_response = {}
                    for i in range(STATUS_POLL_RETRIES):
                        status_backend_response = await send_to_tcp_backend("/status")
                        if status_backend_response.get("status") == "active":
                            call_active_on_backend = True
                            print(f"[WS] Backend сообщил о статусе 'active' после {i+1} попыток.")
                            break
                        print(f"[WS] Backend статус не 'active' ({status_backend_response.get('status')}). Попытка {i+1}/{STATUS_POLL_RETRIES}. Ожидание {STATUS_POLL_INTERVAL} сек...")
                        await asyncio.sleep(STATUS_POLL_INTERVAL)
                    
                    if call_active_on_backend:
                        # Если Backend считает себя активным
                        if current_call_program_process and current_call_program_process.returncode is None:
                            # И наша программа тоже запущена, отклоняем запрос.
                            ws_response = {
                                "status": "error",
                                "message": "Call already active, please hangup first.",
                                "backend_status": status_backend_response
                            }
                            await websocket.send(json.dumps(ws_response))
                            continue
                        else:
                            # Backend активен, но наша программа не запущена/зависла.
                            # Это рассинхронизация. Попытаемся сбросить состояние на backend и запустить заново.
                            print("[WS] Backend считает себя активным, но локальной программы нет или она завершилась. Попытка сброса состояния backend и переинициализации.")
                            await send_to_tcp_backend("/hangup") # Сброс состояния на бэкенде
                            await asyncio.sleep(0.1) # Небольшая задержка перед продолжением


                    # 2. Убить существующий процесс, если он почему-то все еще запущен
                    if current_call_program_process and current_call_program_process.returncode is None:
                        print("[WS] Обнаружен незавершенный процесс звонка. Принудительное завершение перед новым звонком.")
                        await _kill_existing_call_program(force_kill_if_stuck=True)
                        await asyncio.sleep(0.5) # Дать время на очистку

                    # 3. Запуск внешней программы
                    print(f"[WS] Запуск внешней программы '{call_program_path}'...")
                    # >>> ИЗБАВЛЯЕМСЯ ОТ ПОВТОРНОГО GLOBAL ЗДЕСЬ <<<
                    try:
                        current_call_program_process = await asyncio.create_subprocess_exec(
                            *call_program_cmd, # Используем распакованную команду
                            stdin=asyncio.subprocess.PIPE,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE
                        )
                        current_call_program_stdin_writer = current_call_program_process.stdin
                        current_call_program_stdout_reader = current_call_program_process.stdout

                        program_stdout_task = asyncio.create_task(_read_output_from_call_program())
                        program_stderr_task = asyncio.create_task(current_call_program_process.stderr.read())
                        program_stderr_task.add_done_callback(
                            lambda f: print(f"[PROGRAM_ERR] {f.result().decode('utf-8').strip()}") if f.result() else None
                        )

                        print(f"[WS] Программа '{call_program_path}' запущена с PID {current_call_program_process.pid}. Ожидание {PROGRAM_START_DELAY} сек...")
                        await asyncio.sleep(PROGRAM_START_DELAY) # Задержка 3 секунды

                        # 4. Отправка команды "call" запущенной программе
                        call_command_for_program = f"call {number}@{AUDIO_CALL_DOMAIN}"
                        await _send_command_to_call_program(call_command_for_program)

                        # 5. Отправка команды /audio на TCP Backend Server (для синхронизации состояния)
                        backend_response = await send_to_tcp_backend(f"/audio {number}@{AUDIO_CALL_DOMAIN}")
                        ws_response = {
                            "status": "success",
                            "command": "call",
                            "number": number,
                            "backend_response": backend_response,
                            "program_pid": current_call_program_process.pid
                        }
                    except FileNotFoundError:
                        ws_response = {"status": "error", "message": f"Program '{call_program_path}' not found. Make sure it's in the correct path."}
                        print(f"[WS] Ошибка: Программа '{call_program_path}' не найдена.", file=sys.stderr)
                    except Exception as e:
                        ws_response = {"status": "error", "message": f"Failed to start/communicate with call program: {e}"}
                        print(f"[WS] Ошибка при запуске/связи с программой звонка: {e}", file=sys.stderr)
                        await _kill_existing_call_program() # Попытка очистки
                
                elif command == "hangup":
                    # 1. Отправка команды "hangup" на TCP Backend Server
                    backend_response = await send_to_tcp_backend("/hangup")
                    
                    # 2. Отправка команды "hangup" запущенной программе (если есть)
                    if current_call_program_process and current_call_program_process.returncode is None:
                        await _send_command_to_call_program("hangup")
                        # Здесь мы не ждем завершения программы, так как она может ответить "disconnected"
                        # и продолжать работу до команды "quit" или принудительного убийства.

                    ws_response = {
                        "status": "success",
                        "command": "hangup",
                        "backend_response": backend_response
                    }
                    
                # Добавлена обработка "quit" для принудительного убийства программы
                elif command == "quit":
                    print("[WS] Получена команда 'quit'. Принудительное завершение программы звонка.")
                    await _kill_existing_call_program(force_kill_if_stuck=True) # Использовать force_kill
                    ws_response = {
                        "status": "success",
                        "command": "quit",
                        "message": "Call program terminated."
                    }
                else: # Любые другие команды перенаправляются на TCP Backend Server
                    backend_response = await send_to_tcp_backend(f"/{command}")
                    ws_response = {
                        "status": "success",
                        "command": command,
                        "backend_response": backend_response
                    }

            except json.JSONDecodeError:
                ws_response = {
                    "status": "error",
                    "message": "Invalid JSON format."
                }
            except Exception as e:
                # Общая ошибка при обработке запроса
                ws_response = {
                    "status": "error",
                    "message": f"Server processing error: {e}"
                }

            # Отправляем ответ обратно WebSocket клиенту
            await websocket.send(json.dumps(ws_response))
            print(f"[WS] Отправлен ответ на {client_address}: {json.dumps(ws_response)}")

    except websockets.exceptions.ConnectionClosedOK:
        print(f"[WS] Соединение закрыто {client_address} (нормально).")
    except websockets.exceptions.ConnectionClosedError as e:
        print(f"[WS] Соединение закрыто с ошибкой {client_address}: {e}")
    except Exception as e:
        print(f"[WS] Неожиданная ошибка в обработчике WebSocket: {e}")

async def main():
    print(f"Запуск WebSocket сервера на ws://{WS_HOST}:{WS_PORT}")
    print(f"Сервер будет общаться с TCP Backend на {TCP_BACKEND_HOST}:{TCP_BACKEND_PORT}")
    print(f"Сервер будет запускать внешний клиент: {call_program_path}")

    # Убедиться, что процесс очищен при старте
    await _kill_existing_call_program()

    async with websockets.serve(websocket_handler, WS_HOST, WS_PORT):
        # Возможно, здесь можно добавить задачу для периодической проверки "мертвости" программы
        # и очистки, если она завершилась без явной команды
        await asyncio.Future() # Запускаем сервер на неопределенное время

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[SERVER] WebSocket сервер остановлен вручную.")
    except Exception as e:
        print(f"[SERVER] Критическая ошибка при работе сервера: {e}", file=sys.stderr)
    finally:
        # Попытка корректно завершить внешний процесс при выходе сервера
        print("[SERVER] Выполняется очистка запущенных процессов...")
        asyncio.run(_kill_existing_call_program(force_kill_if_stuck=True))
        print("[SERVER] Очистка завершена. Сервер остановлен.")