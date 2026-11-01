# Test storage for mcrsync's rclone panes: a recent rclone serving its
# /data folder as S3 (object storage: no real folders, renames are copy +
# delete) or as plain WebDAV (can't store modification times).
FROM alpine:3.20

ARG RCLONE_VERSION=1.68.2
ADD https://downloads.rclone.org/v${RCLONE_VERSION}/rclone-v${RCLONE_VERSION}-linux-amd64.zip /tmp/rclone.zip
RUN unzip -q /tmp/rclone.zip -d /tmp/rclone \
    && install -m 755 /tmp/rclone/rclone-v${RCLONE_VERSION}-linux-amd64/rclone /usr/local/bin/rclone \
    && rm -rf /tmp/rclone /tmp/rclone.zip \
    && mkdir /data
ENTRYPOINT ["rclone"]
