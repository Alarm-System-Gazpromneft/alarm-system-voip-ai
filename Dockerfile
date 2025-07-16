FROM debian:bookworm

COPY sys/5.15.167.4-microsoft-standard-WSL2+ /lib/modules/5.15.167.4-microsoft-standard-WSL2+

RUN apt update && DEBIAN_FRONTEND=noninteractive TZ=Etc/UTC apt-get -y install tzdata curl kmod
RUN  curl -o /usr/share/keyrings/agp-debian-key.gpg http://download.ag-projects.com/agp-debian-key.gpg
RUN  echo "deb [trusted=yes] https://packages.ag-projects.com/debian bookworm main" >> /etc/apt/sources.list.d/ag-projects.list
RUN  echo "deb-src [trusted=yes] https://packages.ag-projects.com/debian bookworm main" >> /etc/apt/sources.list.d/ag-projects.list
RUN  apt update && apt install -y sipclients3 python3-sipsimple python3-websockets nano pulseaudio-utils pulseaudio

RUN apt install -y python3-pip libasound-dev libportaudio2 espeak espeak-ng libespeak1
RUN pip install pyttsx3 vosk sounddevice numpy --break-system-packages
# ####vosk
# ARG KALDI_MKL

# RUN apt-get update && \
#     apt-get install -y --no-install-recommends \
#         wget \
#         bzip2 \
#         unzip \
#         xz-utils \
#         g++ \
#         make \
#         cmake \
#         git \
#         python3 \
#         python3-dev \
#         python3-websockets \
#         python3-setuptools \
#         python3-pip \
#         python3-wheel \
#         python3-cffi \
#         zlib1g-dev \
#         automake \
#         autoconf \
#         libtool \
#         pkg-config \
#         ca-certificates \
#     && rm -rf /var/lib/apt/lists/*

# RUN \
#     git clone -b vosk --single-branch https://github.com/alphacep/kaldi /opt/kaldi \
#     && cd /opt/kaldi/tools \
#     && sed -i 's:status=0:exit 0:g' extras/check_dependencies.sh \
#     && sed -i 's:--enable-ngram-fsts:--enable-ngram-fsts --disable-bin:g' Makefile \
#     && make -j $(nproc) openfst cub \
#     && if [ "x$KALDI_MKL" != "x1" ] ; then \
#           extras/install_openblas_clapack.sh; \
#        else \
#           extras/install_mkl.sh; \
#        fi \
#     \
#     && cd /opt/kaldi/src \
#     && if [ "x$KALDI_MKL" != "x1" ] ; then \
#           ./configure --mathlib=OPENBLAS_CLAPACK --shared; \
#        else \
#           ./configure --mathlib=MKL --shared; \
#        fi \
#     && sed -i 's:-msse -msse2:-msse -msse2:g' kaldi.mk \
#     && sed -i 's: -O1 : -O3 :g' kaldi.mk \
#     && make -j $(nproc) online2 lm rnnlm \
#     \
#     && git clone https://github.com/alphacep/vosk-api /opt/vosk-api \
#     && cd /opt/vosk-api/src \
#     && KALDI_MKL=$KALDI_MKL KALDI_ROOT=/opt/kaldi make -j $(nproc) \
#     && cd /opt/vosk-api/python \
#     && python3 ./setup.py install \
#     \
#     && git clone https://github.com/alphacep/vosk-server /opt/vosk-server \
#     \
#     && rm -rf /opt/vosk-api/src/*.o \
#     && rm -rf /opt/kaldi \
#     && rm -rf /root/.cache \
#     && rm -rf /var/lib/apt/lists/*
# ####vosk

COPY sys/sip-session3.py /usr/bin/sip-session3
COPY sys/ui.py /usr/lib/python3/dist-packages/sipclient/ui.py

RUN  chmod +x /usr/bin/sip-session3
COPY src /app

RUN  sip-settings3 --account add 200@fekeniyibklof.beget.app test200
RUN  sip-settings3 --account default 200@fekeniyibklof.beget.app

RUN chmod +x /app/entrypoint.sh
CMD ["/app/entrypoint.sh"]