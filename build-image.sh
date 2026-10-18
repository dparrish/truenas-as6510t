#!/bin/sh
set -eu
IMAGE=${IMAGE:-asustor-frontpanel:0.1.0}
exec docker build --tag "$IMAGE" .
