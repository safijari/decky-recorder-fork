#!/bin/sh
set -e

OUTDIR="/backend/out"

cd /backend

cp -r /pacman/usr/lib/* /backend/out

cp -r /psutil /backend/out/