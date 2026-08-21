FROM vulhub/ofbiz:18.12.14

COPY postgresql-42.7.7.jar /usr/src/apache-ofbiz/lib/
RUN rm -f /usr/src/apache-ofbiz//framework/entity/lib/postgresql-9.1-901.jdbc4.jar
COPY postgresql-42.7.7.jar /usr/src/apache-ofbiz/framework/entity/lib/
COPY entityengine.xml.default /tmp/entityengine.xml.default
COPY entityengine.xml.postgres /tmp/entityengine.xml.postgres
COPY build.gradle /tmp/build.gradle
COPY build.gradle.plugin /tmp/build.gradle.plugin
COPY ./url.properties /usr/src/apache-ofbiz/framework/webapp/config/url.properties 


COPY ./hook /hook

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["java", "-Xbootclasspath/a:/hook/java-agent.jar", "-javaagent:/hook/java-agent.jar", "-jar", "./build/libs/ofbiz.jar"]
