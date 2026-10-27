# Python 3.4 on Debian stretch - same Python as the Synology chroot, and
# rsync 3.1.2 / OpenSSH 7.4, the oldest versions mcrsync supports.
FROM python:3.4-slim

RUN printf 'deb http://archive.debian.org/debian stretch main\n' > /etc/apt/sources.list \
    && apt-get -o Acquire::Check-Valid-Until=false update \
    && apt-get install -y --no-install-recommends --allow-unauthenticated rsync openssh-client tmux \
    && rm -rf /var/lib/apt/lists/*

COPY client/entrypoint.sh /entrypoint.sh
RUN sed -i 's/\r$//' /entrypoint.sh && chmod +x /entrypoint.sh
ENV LANG=C.UTF-8 TERM=xterm-256color
WORKDIR /app
ENTRYPOINT ["/entrypoint.sh"]
CMD ["bash"]
