# websocket_client.py
import asyncio
import websockets
import json

# URL нашего WebSocket-сервер
WS_URL = "ws://192.168.218.37:8765"

async def send_command(websocket, command_data):
    """Отправляет команду на сервер и печатает ответ."""
    try:
        # Сериализуем команду в JSON-строку
        message_to_send = json.dumps(command_data)
        print(f"\n[CLIENT] Отправка: {message_to_send}")

        # Отправляем сообщение
        await websocket.send(message_to_send)

        # Ждем и получаем ответ
        response_message = await websocket.recv()
        print(f"[CLIENT] Получен ответ: {response_message}")
        try:
            json_response = json.loads(response_message)
            print("[CLIENT] Десериализованный ответ:")
            print(json.dumps(json_response, indent=2, ensure_ascii=False))
        except json.JSONDecodeError:
            print("[CLIENT] Ответ не является валидным JSON.")

    except websockets.exceptions.ConnectionClosedOK:
        print("[CLIENT] Соединение закрыто сервером (нормально).")
    except websockets.exceptions.ConnectionClosedError as e:
        print(f"[CLIENT] Соединение закрыто с ошибкой: {e}")
    except Exception as e:
        print(f"[CLIENT] Произошла ошибка при отправке/получении: {e}")

async def main():
    print(f"Попытка подключения к WebSocket-серверу на {WS_URL}...")
    try:
        async with websockets.connect(WS_URL) as websocket:
            print(f"[CLIENT] Успешное подключение к {WS_URL}")

            # --- Тест 1: Запрос статуса ---
            #await send_command(websocket, {"command": "status"})
            #await asyncio.sleep(1) # Небольшая задержка, чтобы увидеть ответы по отдельности

            # --- Тест 2: Запрос звонка ---
            #await send_command(websocket, {"command": "call", "number": 101})
            #await asyncio.sleep(5)


            # --- Тест 5: Неизвестная команда ---
            await send_command(websocket, {"command": "status"})
            #await asyncio.sleep(5)
            #await send_command(websocket, {"command": "hangup"})
            #await asyncio.sleep(3)
            #await send_command(websocket, {"command": "status"})
            # --- Тест 6: Невалидный JSON (с точки зрения клиента, это будет отправлено как строка) ---
            # await websocket.send("это не json") # Сервер вернет ошибку "Invalid JSON format."
            # response_message = await websocket.recv()
            # print(f"\n[CLIENT] Ответ на невалидный JSON: {response_message}")

            print("\n[CLIENT] Все команды отправлены.")

    except ConnectionRefusedError:
        print(f"[CLIENT] Ошибка: Не удалось подключиться к серверу по {WS_URL}. Убедитесь, что сервер запущен.")
    except Exception as e:
        print(f"[CLIENT] Произошла непредвиденная ошибка при подключении: {e}")

if __name__ == "__main__":
    asyncio.run(main())
