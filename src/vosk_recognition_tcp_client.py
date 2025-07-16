# vosk_recognition_tcp_client_simple.py
import argparse
import queue
import sys
import json
import sounddevice as sd
import socket
import threading
import time
import os

from vosk import Model, KaldiRecognizer
import numpy as np

# --- Конфигурация для Vosk-клиента ---
VOSK_MODEL_PATH = "/app/vosk-model-small-ru-0.22" # <-- УКАЖИТЕ ПУТЬ К ВАШЕЙ МОДЕЛИ
VOSK_MODEL_SAMPLE_RATE = 16000 # Частота, на которой тренирована Vosk модель (обычно 16000)

# Устройство ввода звука. Используйте sd.query_devices() для поиска ID или имени
AUDIO_INPUT_DEVICE_ID = 1 # <-- УКАЖИТЕ ID/ИМЯ ВАШЕГО АУДИО УСТРОЙСТВА

# Куда отправлять результаты распознавания (адрес proxy_server'а)
RESULTS_SEND_HOST = "127.0.0.1"
RESULTS_SEND_PORT = 9991 # Порт, куда отправляются результаты распознавания

# --- Глобальные переменные ---
audio_q = queue.Queue() # Очередь для аудиоданных из sounddevice callback
send_q = queue.Queue() # Очередь для распознанных результатов, которые нужно отправить по TCP
stop_event = threading.Event() # Событие для сигнализации об остановке


def _audio_callback(indata, frames, time_info, status):
    """
    Callback-функция sounddevice для захвата аудио. Вызывается в отдельном потоке sounddevice.
    """
    if status:
        print(f"[VOSK_AUDIO_CB_STATUS] {status}", file=sys.stderr)
    
    # Всегда помещаем аудиоданные в очередь, пока программа работает
    audio_q.put(indata.copy())

def _send_results_to_proxy_thread_target():
    """
    Поток, который постоянно отправляет результаты из send_q на proxy_server по TCP.
    """
    print(f"[SENDER] Поток отправки результатов запущен. Цель: {RESULTS_SEND_HOST}:{RESULTS_SEND_PORT}", file=sys.stderr)
    while not stop_event.is_set():
        try:
            result_json = send_q.get(timeout=0.1) # Ждем результат с таймаутом
            
            # Устанавливаем новое соединение для каждого результата.
            # Для надежности и простоты в этой демонстрации.
            # В высоконагруженных системах можно переиспользовать одно соединение.
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(1) # Таймаут на подключение/отправку
                sock.connect((RESULTS_SEND_HOST, RESULTS_SEND_PORT))
                message = json.dumps(result_json) + '\n'
                sock.sendall(message.encode('utf-8'))
                # print(f"[SENDER] Отправлено: {message.strip()}", file=sys.stderr)
        except queue.Empty:
            continue # Таймаут, очередь пуста
        except socket.error as e:
            print(f"[SENDER_ERR] Ошибка сети при отправке: {e}. Прокси-сервер запущен?", file=sys.stderr)
            time.sleep(1) # Короткая пауза перед повторной попыткой
        except Exception as e:
            print(f"[SENDER_ERR] Неожиданная ошибка в потоке отправки: {e}", file=sys.stderr)
            time.sleep(1)
    print("[SENDER] Поток отправки результатов завершен.", file=sys.stderr)


def main():
    global vosk_model # Теперь модель будет загружаться один раз в main

    print(f"Vosk Recognition TCP Client (Simple) запущен. PID: {os.getpid()}", file=sys.stderr)
    print(f"Будет отправлять результаты на {RESULTS_SEND_HOST}:{RESULTS_SEND_PORT}", file=sys.stderr)
    print(f"Использует Vosk модель из: {VOSK_MODEL_PATH}", file=sys.stderr)

    # --- 1. Загрузка Vosk модели ---
    try:
        vosk_model = Model(VOSK_MODEL_PATH)
        print("[VOSK] Vosk модель загружена.", file=sys.stderr)
    except Exception as e:
        print(f"[VOSK_ERR] Ошибка загрузки Vosk модели: {e}. Убедитесь, что путь '{VOSK_MODEL_PATH}' верен и модель полная.", file=sys.stderr)
        sys.exit(1)

    # --- 2. Получение информации об аудиоустройстве ---
    try:
        device_info = sd.query_devices(AUDIO_INPUT_DEVICE_ID, 'input')
        device_id_to_use = device_info['index']
        num_channels_device = device_info['max_input_channels']
        actual_samplerate = int(device_info['default_samplerate'])
        
        if num_channels_device <= 0:
            raise ValueError(f"Устройство '{AUDIO_INPUT_DEVICE_ID}' не является устройством ввода или не имеет каналов.")
        print(f"[VOSK] Используем аудиоустройство: {device_info['name']} (ID: {device_id_to_use}, Каналы: {num_channels_device}, Частота: {actual_samplerate} Hz)", file=sys.stderr)
    except Exception as e:
        print(f"[VOSK_ERR] Не удалось найти или настроить аудиоустройство '{AUDIO_INPUT_DEVICE_ID}': {e}", file=sys.stderr)
        print("[VOSK_ERR] Доступные устройства ввода:", file=sys.stderr)
        for i, dev in enumerate(sd.query_devices()):
            if dev['max_input_channels'] > 0:
                print(f"  ID: {i}, Name: {dev['name']}, Channels: {dev['max_input_channels']}, Default_SR: {dev['default_samplerate']}", file=sys.stderr)
        sys.exit(1)

    # --- 3. Инициализация KaldiRecognizer ---
    rec = KaldiRecognizer(vosk_model, VOSK_MODEL_SAMPLE_RATE)

    # --- 4. Запуск потока для отправки результатов по TCP ---
    sender_thread = threading.Thread(target=_send_results_to_proxy_thread_target, daemon=True)
    sender_thread.start()

    # --- 5. Запуск основного цикла распознавания ---
    try:
        # Открываем аудиопоток. Он будет работать постоянно.
        with sd.InputStream(samplerate=actual_samplerate, device=device_id_to_use,
                            channels=num_channels_device, dtype='int16', callback=_audio_callback):
            print("[VOSK] Vosk распознавание запущено. Аудиопоток активен. Говорите в микрофон.", file=sys.stderr)
            print("Для остановки нажмите Ctrl+C.", file=sys.stderr)
            
            while not stop_event.is_set():
                try:
                    data = audio_q.get(timeout=0.1) # Ждем аудио из очереди с таймаутом

                    # Если устройство многоканальное, берем только первый канал для Vosk
                    if data.ndim > 1:
                        data_mono = data[:, 0]
                    else:
                        data_mono = data

                    # Подаем данные в распознаватель
                    if rec.AcceptWaveform(data_mono.tobytes()):
                        result_final = json.loads(rec.Result())
                        if result_final.get('text'):
                            send_q.put({"event": "recognition_final", "text": result_final["text"]})
                            print(f"[VOSK_FINAL] {result_final['text']}", file=sys.stderr)
                    else:
                        partial_result = json.loads(rec.PartialResult())
                        if partial_result.get('partial'):
                            send_q.put({"event": "recognition_partial", "text": partial_result["partial"]})
                            # print(f"[VOSK_PARTIAL] {partial_result['partial']}", end='\r', file=sys.stderr) # Отладочный вывод

                except queue.Empty:
                    continue # Таймаут очереди, просто продолжаем ждать
                except Exception as e:
                    print(f"[VOSK_ERR] Неожиданная ошибка в Vosk распознавании: {e}", file=sys.stderr)
                    break # Выход из цикла, если произошла ошибка
            
            # Получаем окончательный результат при остановке (если была незавершенная фраза)
            final_result_on_stop = json.loads(rec.FinalResult())
            if final_result_on_stop.get('text'):
                send_q.put({"event": "recognition_final_on_stop", "text": final_result_on_stop["text"]})
                print(f"[VOSK_FINAL_ON_STOP] {final_result_on_stop['text']}", file=sys.stderr)

    except KeyboardInterrupt:
        print("\n[VOSK_CLIENT] Распознавание остановлено вручную (Ctrl+C).", file=sys.stderr)
    except sd.PortAudioError as e:
        print(f"[VOSK_ERR] Ошибка PortAudio: {e}", file=sys.stderr)
    except Exception as e:
        print(f"[VOSK_ERR] Критическая ошибка Vosk клиента: {e}", file=sys.stderr)
    finally:
        print("[VOSK_CLIENT] Выполняется очистка ресурсов...", file=sys.stderr)
        stop_event.set() # Сигнализируем всем потокам об остановке
        if sender_thread and sender_thread.is_alive():
            sender_thread.join(timeout=5) # Ждем завершения потока отправки
        print("[VOSK_CLIENT] Очистка завершена. Программа остановлена.", file=sys.stderr)

if __name__ == "__main__":
    main()