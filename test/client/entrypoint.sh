#!/bin/sh
# The shared key volume is world-readable; ssh insists on a private copy.
mkdir -p /root/.ssh
cp /keys/id_test /root/.ssh/id_test
chmod 600 /root/.ssh/id_test
exec "$@"
