# Based on the official Flowable UI image
FROM flowable/flowable-ui:latest

USER root

RUN sed -i 's/dl-cdn.alpinelinux.org/mirrors.aliyun.com/g' /etc/apk/repositories && \
    apk update && \
    apk add --no-cache libgcc libstdc++ gcompat

USER flowable