ARG BASE_IMAGE=python:3.13-slim
FROM ${BASE_IMAGE}
COPY qqmusic-service/app.py qqmusic-service/requirements.txt /srv/
COPY webui-service/static/ /srv/static/
COPY container/lxserver-artifact/ /srv/lxserver-artifact/
