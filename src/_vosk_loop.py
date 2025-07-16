import argparse
import queue
import sys
import json
import sounddevice as sd
import socket
from vosk import Model, KaldiRecognizer

# Создаем очередь для обмена данными между потоками
q = queue.Queue()
RESULTS_SEND_HOST = "127.0.0.1"
RESULTS_SEND_PORT = 9991 # Порт, куда отправляются результаты распознавания

def list_audio_devices():
    """Выводит список доступных аудиоустройств."""
    print("Доступные аудиоустройства:")
    try:
        devices = sd.query_devices()
        input_devices = []
        for i, device in enumerate(devices):
            # Проверяем, является ли устройство устройством ввода
            if device['max_input_channels'] > 0:
                print(f"  {len(input_devices)}: {device['name']}")
                input_devices.append(i)
        return input_devices
    except Exception as e:
        print(f"Не удалось получить список устройств: {e}")
        return []

def select_device(device_id, available_devices):
    """Выбирает устройство по ID или запрашивает у пользователя."""
    if device_id is not None:
        try:
            return available_devices[device_id]
        except IndexError:
            print(f"Ошибка: Неверный номер устройства '{device_id}'.")
            sys.exit(1)
    else:
        while True:
            try:
                choice = int(input("Выберите номер устройства для записи: "))
                if 0 <= choice < len(available_devices):
                    return available_devices[choice]
                else:
                    print("Неверный номер. Попробуйте еще раз.")
            except ValueError:
                print("Пожалуйста, введите число.")


def callback(indata, frames, time, status):
    """
    Эта функция вызывается для каждого блока аудио из потока.
    `indata` содержит аудиоданные.
    `status` сообщает об ошибках.
    """
    if status:
        print(status, file=sys.stderr)
    # Помещаем блок аудиоданных в очередь
    q.put(bytes(indata))

def main():
    # --- 1. Обработка аргументов командной строки ---
    parser = argparse.ArgumentParser(description="Непрерывное распознавание речи с Vosk и выбором аудиоканала.")
    parser.add_argument(
        "-l", "--list-devices", action="store_true",
        help="Показать список доступных аудиоустройств и выйти"
    )
    parser.add_argument(
        "-d", "--device", type=int,
        help="Номер устройства ввода (микрофона) для использования"
    )
    parser.add_argument(
        "-m", "--model", type=str, default="model",
        help="Путь к папке с моделью Vosk"
    )
    args = parser.parse_args()

    # --- 2. Выбор аудиоустройства ---
    available_devices_indices = list_audio_devices()
    
    if args.list_devices:
        sys.exit(0) # Если запросили только список, выходим
    
    if not available_devices_indices:
        print("Не найдено ни одного устройства ввода. Выход.")
        sys.exit(1)

    device_index = select_device(args.device, available_devices_indices)

    # --- 3. Инициализация модели Vosk ---
    try:
        model = Model(args.model)
    except Exception:
        print(f"Не удалось загрузить модель из '{args.model}'.")
        print("Убедитесь, что вы скачали модель и указали правильный путь.")
        print("Скачать модели можно здесь: https://alphacephei.com/vosk/models")
        sys.exit(1)

    # Получаем частоту дискретизации из информации об устройстве
    try:
        device_info = sd.query_devices(device_index, 'input')
        samplerate = int(device_info['default_samplerate'])
        print(samplerate)
    except Exception as e:
        print(f"Не удалось получить частоту дискретизации для устройства, используем частоту модели. Ошибка: {e}")
        samplerate = int(model.samplerate)


    # --- 4. Основной цикл распознавания ---
    print("\nНачинаем распознавание. Говорите в микрофон.")
    print("Для остановки нажмите Ctrl+C.")
    
    try:
        # Создаем распознаватель
        recognizer = KaldiRecognizer(model, samplerate)
        
        # Открываем аудиопоток с выбранного устройства
        with sd.InputStream(samplerate=samplerate, device=device_index,
                            channels=1, dtype='int16', callback=callback):
            while True:
                # Получаем данные из очереди
                data = q.get()
                
                # Подаем данные в распознаватель
                if recognizer.AcceptWaveform(data):
                    # Если распознаватель вернул True, значит, он считает фразу законченной
                    result = json.loads(recognizer.Result())
                    if result['text']:
                        print(f"Распознано: {result['text']}")
                        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                            sock.settimeout(1) # Таймаут на подключение/отправку
                            sock.connect((RESULTS_SEND_HOST, RESULTS_SEND_PORT))
                            message = json.dumps({"event": "recognition_partial", "text": result['text']}) + '\n'
                            sock.send(message.encode('utf-8'))
                else:
                    # Иначе это частичный результат (в процессе речи)
                    partial_result = json.loads(recognizer.PartialResult())
                    # Выводим частичный результат на той же строке
                    # print(f"  ... {partial_result['partial']}", end='\r')

    except KeyboardInterrupt:
        print("\nРаспознавание остановлено.")
    except Exception as e:
        print(f"Произошла ошибка: {type(e).__name__}: {e}")

if __name__ == "__main__":
    main()