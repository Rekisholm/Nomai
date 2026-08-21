#!/bin/bash
set -e

# Database initialization check logic
if [ "$USE_POSTGRES" = "true" ]; then
    echo "Using PostgreSQL config"
    # Check whether the PostgressDemo plugin exists
    if [ ! -d "/usr/src/apache-ofbiz/plugins/PostgressDemo" ]; then
        echo "PostgressDemo plugin not found, creating it..."
        cp  /tmp/entityengine.xml.postgres \
            /usr/src/apache-ofbiz/framework/entity/config/entityengine.xml
        echo "Creating PostgressDemo plugin"
        ./gradlew createPlugin -PpluginId=PostgressDemo --stacktrace
        echo "Copying build.gradle files for PostgreSQL"
        cp /tmp/build.gradle /usr/src/apache-ofbiz/build.gradle
        cp /tmp/build.gradle.plugin /usr/src/apache-ofbiz/plugins/PostgressDemo/build.gradle
        echo "Starting Apache OFBiz build process..."
        ./gradlew cleanAll loadAll --stacktrace
    else
        echo "Postgres Data already exists, skipping creation"
    fi

else
    echo "Using default Derby config"
    cp /tmp/entityengine.xml.default \
       /usr/src/apache-ofbiz/framework/entity/config/entityengine.xml
fi


exec "$@"
