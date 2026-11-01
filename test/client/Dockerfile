# Python 3.4 on Debian stretch - same Python as the Synology chroot, and
# rsync 3.1.2 / OpenSSH 7.4 / rclone 1.60, the oldest versions mcrsync
# supports. Build with --build-arg RCLONE_VERSION=x.y.z to test another rclone.
FROM python:3.4-slim

RUN printf 'deb http://archive.debian.org/debian stretch main\n' > /etc/apt/sources.list \
    && apt-get -o Acquire::Check-Valid-Until=false update \
    && apt-get install -y --no-install-recommends --allow-unauthenticated rsync openssh-client tmux \
    && rm -rf /var/lib/apt/lists/*

ARG RCLONE_VERSION=1.60.1
ADD https://downloads.rclone.org/v${RCLONE_VERSION}/rclone-v${RCLONE_VERSION}-linux-amd64.zip /tmp/rclone.zip
RUN python3 -c "import zipfile; zipfile.ZipFile('/tmp/rclone.zip').extractall('/tmp/rclone')" \
    && install -m 755 /tmp/rclone/rclone-v${RCLONE_VERSION}-linux-amd64/rclone /usr/local/bin/rclone \
    && rm -rf /tmp/rclone /tmp/rclone.zip

COPY client/entrypoint.sh /entrypoint.sh
RUN sed -i 's/\r$//' /entrypoint.sh && chmod +x /entrypoint.sh
ENV LANG=C.UTF-8 TERM=xterm-256color
WORKDIR /app
ENTRYPOINT ["/entrypoint.sh"]
CMD ["bash"]
