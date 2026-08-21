FROM kanboard/kanboard:v1.2.44

RUN apk add php83-ffi php82-ffi libffi gcompat

RUN echo "ffi.enable=true" >> /etc/php82/conf.d/00_ffi.ini

RUN echo "ffi.enable=true" >> /etc/php83/conf.d/00_ffi.ini

RUN sed -i '/^exec /i mkdir -p /tmp/logs \&\& chmod 777 /tmp/logs' /usr/local/bin/entrypoint.sh
