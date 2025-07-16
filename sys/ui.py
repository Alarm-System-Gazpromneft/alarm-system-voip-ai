"""
Implements a library that can be used for buildina a fully featured SIP
User Agent. This version is stripped of TTY (terminal) interaction.
It focuses on background processing, TCP command handling, and FIFO tailing.
"""

__all__ = ["UI"] # Removed RichText, CompoundRichText, Prompt, Question

import pickle as pickle # pickle is for history, which is removed now as Input is removed
import os
import re
import signal # Signal for graceful shutdown (not WINCH anymore)
import sys
from collections import deque # Not needed if questions are removed
from threading import RLock, Thread
import time
import socket

from application.python.decorator import decorator, preserve_signature
from application.python.queue import EventQueue
from application.python.types import Singleton
from application.system import openfile
from application.notification import NotificationCenter, NotificationData


@decorator
def run_in_ui_thread(func):
    @preserve_signature(func)
    def wrapper(self, *args, **kwargs):
        # The UI is now primarily an event dispatcher.
        # All actions that modify its internal state or send notifications
        # should go through the event queue to ensure thread safety.
        self.event_queue.put((func, self, args, kwargs))
    return wrapper


# Removed RichText, CompoundRichText, Prompt, Question classes.
# These were purely for terminal display and formatting.


# Removed Input class.
# This class managed terminal line editing and history specific to TTY interaction.


class UI(Thread, metaclass=Singleton):
    # Removed control_chars as no longer processing terminal input

    # public functions
    #

    def __init__(self):
        Thread.__init__(self, target=self._run, name='UI-Thread')
        self.setDaemon(True)

        self.__dict__['prompt'] = '' # No longer a Prompt object, just a string
        self.__dict__['status'] = None
        self.command_sequence = '/'
        self.application_control_char = '\x18' # ctrl-X - these might be removed if no local input
        self.application_control_bindings = {} # No local input, so bindings are moot
        self.display_commands = True # Only for console output, not interactive
        self.display_text = True     # Only for console output, not interactive

        # Removed cursor_x, cursor_y, displaying_question, last_window_size, prompt_y
        # Removed questions deque

        self.stopping = False
        self.lock = RLock()
        self.event_queue = EventQueue(handler=lambda function_self_args_kwargs: function_self_args_kwargs[0](function_self_args_kwargs[1], *function_self_args_kwargs[2], **function_self_args_kwargs[3]), name='UI operation handling')

        self.tcp_server_socket = None
        self.tcp_server_thread = None

    def start(self, prompt='', command_sequence='/', control_char='\x18', control_bindings={}, display_commands=True, display_text=True, tty_log_file=None, tcp_host='127.0.0.1', tcp_port=9999):
        with self.lock:
            if self.is_alive():
                raise RuntimeError('UI already active')

            self.command_sequence = command_sequence
            self.application_control_char = control_char # Still here but unused without TTY
            self.application_control_bindings = control_bindings # Still here but unused without TTY
            self.display_commands = display_commands
            self.display_text = display_text

            try:
                # Создаем и настраиваем сокет
                self.tcp_server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.tcp_server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self.tcp_server_socket.bind((tcp_host, tcp_port))
                self.tcp_server_socket.listen(5)

                # Запускаем сервер в отдельном daemon-потоке, чтобы не блокировать UI
                self.tcp_server_thread = Thread(target=self._tcp_server_loop, daemon=True)
                self.tcp_server_thread.start()
                self.write(f"[*] TCP command server started on {tcp_host}:{tcp_port}")

            except Exception as e:
                self.write(f"[!] Failed to start TCP server: {e}")
                self.tcp_server_socket = None

            # Removed TTY-specific setup:
            # - termios changes
            # - cursor position queries
            # - sys.stdout/stderr wrapping (TTYFileWrapper is removed)
            # - SIGWINCH signal handling

            self.event_queue.start()
            Thread.start(self)

            # This will trigger the update of the prompt - but now it's just setting an attribute
            self.prompt = prompt

    @run_in_ui_thread
    def stop(self):
        with self.lock:
            self.stopping = True

            if hasattr(self, 'tcp_server_socket') and self.tcp_server_socket:
                self.tcp_server_socket.close() # This unblocks .accept() in the server thread
                self.write("[*] TCP server stopped.")

            self.status = None
            # Removed TTY-specific cleanup:
            # - sys.stdout.send_to_file()
            # - sys.stderr.send_to_file()
            # - _raw_write('\n\x1b[2K')
            # - input.save_history() (Input class removed)

    def _tcp_server_loop(self):
        """
        Основной цикл сервера. Принимает новые подключения
        и для каждого запускает отдельный поток-обработчик.
        """
        while not self.stopping:
            try:
                client_socket, client_address = self.tcp_server_socket.accept()
                self.write(f"[*] Accepted connection from {client_address[0]}:{client_address[1]}")
                
                client_handler = Thread(target=self._handle_tcp_client, args=(client_socket, client_address), daemon=True)
                client_handler.start()
            except OSError:
                # Error will occur when socket is closed in stop()
                break
            except Exception as e:
                # General error handling, might be socket closed prematurely
                pass # self.write(f"[!] TCP server error: {e}")

    def _handle_tcp_client(self, client_socket, client_address):
        """
        Обрабатывает одного клиента: читает данные, парсит команды и исполняет их.
        """
        buffer = ""
        notification_center = NotificationCenter()

        try:
            with client_socket:
                def tcp_responder(message):
                    try:
                        client_socket.sendall((str(message) + '\n').encode('utf-8'))
                    except (OSError, BrokenPipeError):
                        pass
                while not self.stopping:
                    data = client_socket.recv(1024)
                    if not data:
                        break # Client closed connection

                    buffer += data.decode('utf-8', errors='ignore')

                    while '\n' in buffer:
                        line, buffer = buffer.split('\n', 1)
                        line = line.strip()
                        if not line:
                            continue
                        
                        if line.startswith(self.command_sequence):
                            self.write(f"[*] TCP command: {line}")
                            words = [word for word in re.split(r'\s+', line[len(self.command_sequence):]) if word]
                            if len(words) > 0:
                                notification_data = NotificationData(command=words[0], args=words[1:])
                                notification_data.responder = tcp_responder
                                notification_center.post_notification('UIInputGotCommand', sender=self, data=notification_data)
                        else:
                            notification_data = NotificationData(text=line)
                            notification_data.responder = tcp_responder
                            notification_center.post_notification('UIInputGotText', sender=self, data=notification_data)
        finally:
            self.write(f"[*] Connection from {client_address[0]}:{client_address[1]} closed.")

    def write(self, text):
        # Now simply writes to standard output, without TTY specific buffering or cursor manipulation.
        sys.stdout.write(str(text) + '\n')
        sys.stdout.flush()

    @run_in_ui_thread
    def writelines(self, text_lines):
        # Now simply writes to standard output, without TTY specific buffering or cursor manipulation.
        if not text_lines:
            return
        for text in text_lines:
            sys.stdout.write(str(text) + '\n')
        sys.stdout.flush()

    # Removed add_question and remove_question as questions were for interactive TTY.

    # properties
    #

    # Removed window_size property as it was TTY specific.

    def _get_prompt(self):
        return self.__dict__['prompt']
    @run_in_ui_thread
    def _set_prompt(self, value):
        with self.lock:
            # No longer needs to be a Prompt object or call _update_prompt for TTY display.
            self.__dict__['prompt'] = str(value)
            # The prompt value is now just an internal state, not actively displayed on a terminal.
    prompt = property(_get_prompt, _set_prompt)
    del _get_prompt, _set_prompt

    def _get_status(self):
        return self.__dict__['status']
    @run_in_ui_thread
    def _set_status(self, status):
        with self.lock:
            self.__dict__['status'] = status
            # Status is also just an internal state or can be logged, not drawn on TTY.
    status = property(_get_status, _set_status)
    del _get_status, _set_status


    # private functions
    #

    def _run(self):
        # The main loop for the UI thread.
        # It now primarily services the event queue.
        # All TTY input handling (select.select, os.read, control char parsing) is removed.
        # TCP server and FIFO tailing run in separate threads managed by UI.
        while not self.stopping:
            # The EventQueue.get() call is blocking, and processes events in this thread.
            # This allows other threads to submit tasks to the UI thread safely.
            # No direct blocking on sys.stdin.fileno() anymore.
            time.sleep(0.01) # Small sleep to prevent busy-waiting if event queue is empty but not blocking.
            # The EventQueue's handler processes events, so nothing explicit needs to be done here
            # beyond keeping the thread alive and allowing the event loop to run.

    # Removed _raw_write, _window_resized, _update_prompt, _draw_status, _scroll_up.
    # These were all TTY-specific display functions.

    def _tail_file(self, filepath):
        """
        Читает строки из файла (FIFO), анализирует их и либо исполняет как команды,
        либо выводит как текст.
        """
        try:
            # Now logs to stdout instead of manipulating sys.__stdout__ directly to avoid TTY issues.
            self.write(f"[*] Tailing file {filepath} for commands...")

            with open(filepath, 'r') as f:
                f.seek(0, 2)
                while not self.stopping:
                    line = f.readline()
                    if not line:
                        time.sleep(0.1) # Wait for new data
                        continue
                    
                    line = line.strip()
                    if not line:
                        continue
                    
                    notification_center = NotificationCenter()
                    
                    if line.startswith(self.command_sequence):
                        self.write(f"[*] Received remote command (from FIFO): {line}")
                        words = [word for word in re.split(r'\s+', line[len(self.command_sequence):]) if word]
                        if len(words) > 0:
                            notification_center.post_notification('UIInputGotCommand', sender=self, data=NotificationData(command=words[0], args=words[1:]))
                    else:
                        notification_center.post_notification('UIInputGotText', sender=self, data=NotificationData(text=line))

        except Exception as e:
            self.write(f"[!] Error tailing file {filepath}: {e}")

    # Removed all _CH_ (control character handlers) as there's no TTY input.