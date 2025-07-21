#!/bin/bash
cd /app

modprobe snd-aloop 

# sleep 10000
pulseaudio -D --exit-idle-time=-1
sleep 2
pulseaudio -D --exit-idle-time=-1
sleep 2
pulseaudio -D --exit-idle-time=-1
sleep 2
pactl load-module module-null-sink sink_name=virtual_sorc sink_properties=device.description="Virtual_sorc"
pactl set-default-source virtual_sorc.monitor
# paplay '/app/song.wav' --device=virtual_sorc
python3 websok.py & python3 _vosk_loop.py --model /app/vosk-model-ru-0.42 -d 1
# sleep 10000
