FROM python@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f
ARG KLIPPER_REV=e2b99a9159f992169c1ba53029dec020e850cc99
ARG MOONRAKER_REV=9e676eba6b02661a4dfa3ec6e7ac3f3504498e6d
ARG SIMULAVR_REV=32985f745c237bf8dcd2718235d01c8b1fb0491d
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates build-essential cmake swig gcc-avr avr-libc libelf-dev libffi-dev libsodium23 && rm -rf /var/lib/apt/lists/*
RUN git init /opt/simulavr && cd /opt/simulavr && git remote add origin https://git.savannah.nongnu.org/git/simulavr.git && git fetch --tags origin ${SIMULAVR_REV} && git checkout --detach FETCH_HEAD && python -m pip install --no-cache-dir setuptools==78.1.1 && cmake -S . -B build -DBUILD_PYTHON=ON -DPYTHON_EXECUTABLE=/usr/local/bin/python && cmake --build build --target sim_static -j2 && cmake --build build -j2
RUN git init /opt/klipper && cd /opt/klipper && git remote add origin https://github.com/Klipper3d/klipper.git && git fetch --depth 1 origin ${KLIPPER_REV} && git checkout --detach FETCH_HEAD && printf 'CONFIG_MACH_AVR=y\nCONFIG_MACH_atmega644p=y\nCONFIG_LOW_LEVEL_OPTIONS=y\nCONFIG_SIMULAVR=y\n' > .config && make olddefconfig && make -j2
RUN python -m venv /opt/klippy-env && /opt/klippy-env/bin/pip install --no-cache-dir -r /opt/klipper/scripts/klippy-requirements.txt && PYTHONPATH=/opt/klipper/klippy /opt/klippy-env/bin/python -c 'import chelper; chelper.get_ffi()'
RUN git init /opt/moonraker && cd /opt/moonraker && git remote add origin https://github.com/Arksine/moonraker.git && git fetch --depth 1 origin ${MOONRAKER_REV} && git checkout --detach FETCH_HEAD && python -m venv /opt/moonraker-env && /opt/moonraker-env/bin/pip install --no-cache-dir /opt/moonraker
RUN useradd --uid 10001 --create-home honeyprint && mkdir -p /data && chown honeyprint:honeyprint /data
LABEL org.honeyprint.klipper.revision=${KLIPPER_REV} org.honeyprint.moonraker.revision=${MOONRAKER_REV} org.honeyprint.simulavr.revision=${SIMULAVR_REV}
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONPATH=/opt/simulavr/build/pysimulavr
USER 10001:10001
WORKDIR /data
CMD ["/opt/klippy-env/bin/python", "/opt/klipper/klippy/klippy.py", "--help"]
