#!/usr/bin/env python3
# Copyright 2021 Canonical
# See LICENSE file for licensing details.

""" Module defining the Charmed operator for the FINOS Legend SDLC Server. """

import base64
import logging
import subprocess

from ops import charm
from ops import framework
from ops import main
from ops import model
import jks
import yaml

from charms.finos_legend_db_k8s.v0 import legend_database
from charms.finos_legend_gitlab_integrator_k8s.v0 import legend_gitlab
from charms.nginx_ingress_integrator.v0 import ingress


logger = logging.getLogger(__name__)

SDLC_CONFIG_FILE_CONTAINER_LOCAL_PATH = "/sdlc-config.yaml"
SDLC_SERVICE_URL_FORMAT = "%(schema)s://%(host)s:%(port)s%(path)s"

SDLC_MAIN_GITLAB_REDIRECT_URL = "%(base_url)s/auth/callback"
SDLC_GITLAB_REDIRECT_URI_FORMATS = [
    SDLC_MAIN_GITLAB_REDIRECT_URL,
    "%(base_url)s/pac4j/login/callback"]

TRUSTSTORE_TYPE_JKS = "jks"
TRUSTSTORE_NAME = "Legend SDLC"
TRUSTSTORE_PASSPHRASE = "Legend SDLC"
TRUSTSTORE_CONTAINER_LOCAL_PATH = "/truststore.jks"

APPLICATION_CONNECTOR_TYPE_HTTP = "http"
APPLICATION_CONNECTOR_TYPE_HTTPS = "https"
APPLICATION_CONNECTOR_PORT_HTTP = 7070
APPLICATION_ADMIN_CONNECTOR_PORT_HTTP = 7076
APPLICATION_ROOT_PATH = "/api"

APPLICATION_LOGGING_FORMAT = (
    "%d{yyyy-MM-dd HH:mm:ss.SSS} %-5p [%thread] %c - %m%n")
VALID_APPLICATION_LOG_LEVEL_SETTINGS = [
    "INFO", "WARN", "DEBUG", "TRACE", "OFF"]

GITLAB_PROJECT_VISIBILITY_PUBLIC = "public"

GITLAB_PROJECT_VISIBILITY_PRIVATE = "private"
GITLAB_REQUIRED_SCOPES = ["openid", "profile", "api"]


class LegendSDLCServerCharm(charm.CharmBase):
    """ Charmed operator for the FINOS Legend SDLC Server. """

    _stored = framework.StoredState()

    def __init__(self, *args):
        super().__init__(*args)

        self._set_stored_defaults()

        # Various charm library consumers:
        self._legend_db_consumer = legend_database.LegendDatabaseConsumer(
            self, relation_name="legend-db")
        self._legend_gitlab_consumer = legend_gitlab.LegendGitlabConsumer(
            self, relation_name="legend-sdlc-gitlab")
        self.ingress = ingress.IngressRequires(
            self, {
                "service-hostname": self.app.name,
                "service-name": self.app.name,
                "service-port": APPLICATION_CONNECTOR_PORT_HTTP})

        # Standard charm lifecycle events:
        self.framework.observe(
            self.on.config_changed, self._on_config_changed)
        self.framework.observe(
            self.on.sdlc_pebble_ready, self._on_sdlc_pebble_ready)

        # DB relation lifecycle events:
        self.framework.observe(
            self.on["legend-db"].relation_joined,
            self._on_db_relation_joined)
        self.framework.observe(
            self.on["legend-db"].relation_changed,
            self._on_db_relation_changed)

        # GitLab integrator lifecycle:
        self.framework.observe(
            self.on["legend-sdlc-gitlab"].relation_joined,
            self._on_legend_gitlab_relation_joined)
        self.framework.observe(
            self.on["legend-sdlc-gitlab"].relation_changed,
            self._on_legend_gitlab_relation_changed)

        # Studio relation events:
        self.framework.observe(
            self.on["legend-sdlc"].relation_joined,
            self._on_studio_relation_joined)
        self.framework.observe(
            self.on["legend-sdlc"].relation_changed,
            self._on_studio_relation_changed)

    def _set_stored_defaults(self) -> None:
        self._stored.set_default(log_level="DEBUG")
        self._stored.set_default(legend_db_credentials={})
        self._stored.set_default(legend_gitlab_credentials={})

    def _on_sdlc_pebble_ready(self, event: framework.EventBase) -> None:
        """Define the SDLC workload using the Pebble API.
        Note that this will *not* start the service, but instead leave it in a
        blocked state until the relevant relations required for it are added.
        """
        # Get a reference the container attribute on the PebbleReadyEvent
        container = event.workload

        # Define an initial Pebble layer configuration
        pebble_layer = {
            "summary": "SDLC layer.",
            "description": "Pebble config layer for FINOS Legend SDLC Server.",
            "services": {
                "sdlc": {
                    "override": "replace",
                    "summary": "sdlc",
                    "command": (
                        # NOTE(aznashwan): starting through bash is required
                        # for the classpath glob (-cp ...) to be expanded:
                        "/bin/sh -c 'java -XX:+ExitOnOutOfMemoryError "
                        "-XX:MaxRAMPercentage=60 -Xss4M -cp /app/bin/*.jar "
                        "-Dfile.encoding=UTF8 "
                        "-Djavax.net.ssl.trustStore=\"%s\" "
                        "-Djavax.net.ssl.trustStorePassword=\"%s\" "
                        "org.finos.legend.sdlc.server.LegendSDLCServer "
                        "server \"%s\"'" % (
                            TRUSTSTORE_CONTAINER_LOCAL_PATH,
                            TRUSTSTORE_PASSPHRASE,
                            SDLC_CONFIG_FILE_CONTAINER_LOCAL_PATH)
                    ),
                    # NOTE(aznashwan): considering the SDLC service expects
                    # a singular config file which already contains all
                    # relevant options in it (some of which will require the
                    # relation with DB/Gitlab to have already been
                    # established), we do not auto-start:
                    "startup": "disabled",
                    # TODO(aznashwan): determine any env vars we could pass
                    # (most notably, things like the RAM percentage etc...)
                    "environment": {},
                }
            },
        }

        # Add intial Pebble config layer using the Pebble API
        container.add_layer("sdlc", pebble_layer, combine=True)

        # NOTE(aznashwan): as mentioned above, we will *not* be auto-starting
        # the service until the relations with DBMan and Gitlab are added:
        # container.autostart()

        self.unit.status = model.BlockedStatus(
            "requires relating to: finos-legend-db-k8s, "
            "finos-legend-gitlab-integrator-k8s")

    def _get_logging_level_from_config(self, option_name) -> str:
        """Fetches the config option with the given name and checks to
        ensure that it is a valid `java.utils.logging` log level.

        Returns None if an option is invalid.
        """
        value = self.model.config[option_name]
        if value not in VALID_APPLICATION_LOG_LEVEL_SETTINGS:
            logger.warning(
                "Invalid Java logging level value provided for option "
                "'%s': '%s'. Valid Java logging levels are: %s. The charm "
                "shall block until a proper value is set.",
                option_name, value, VALID_APPLICATION_LOG_LEVEL_SETTINGS)
            return None
        return value

    def _add_base_service_config_from_charm_config(
            self, sdlc_config: dict = {}) -> model.BlockedStatus:
        """This method adds all relevant SDLC config options into the provided
        dict to be directly rendered to YAML and passed to the SDLC container.

        Returns:
            None if all of the config options derived from the config/relations
            are present and have passed Charm-side valiation steps.
            A `model.BlockedStatus` instance with a relevant message otherwise.
        """
        # Check Mongo-related options:
        mongo_creds = self._stored.legend_db_credentials
        if not mongo_creds:
            return model.BlockedStatus(
                "requires relating to: finos-legend-db-k8s")

        # Check gitlab-related options:
        gitlab_project_visibility = GITLAB_PROJECT_VISIBILITY_PRIVATE
        if self.model.config['gitlab-create-new-projects-as-public']:
            gitlab_project_visibility = GITLAB_PROJECT_VISIBILITY_PUBLIC

        legend_gitlab_creds = self._stored.legend_gitlab_credentials
        if not legend_gitlab_creds:
            return model.BlockedStatus(
                "requires relating to: finos-legend-gitlab-integrator-k8s")
        gitlab_client_id = legend_gitlab_creds['client_id']
        gitlab_client_secret = legend_gitlab_creds[
            'client_secret']
        gitlab_openid_discovery_url = legend_gitlab_creds[
            'openid_discovery_url']
        gitlab_project_tag = self.model.config['gitlab-project-tag']
        gitlab_project_creation_group_pattern = (
            self.model.config['gitlab-project-creation-group-pattern'])

        # Check Java logging options:
        request_logging_level = self._get_logging_level_from_config(
            "server-requests-logging-level")
        server_logging_level = self._get_logging_level_from_config(
            "server-logging-level")
        if not all([server_logging_level, request_logging_level]):
            return model.BlockedStatus(
                "one or more logging config options are improperly formatted "
                "or missing, please review the debug-log for more details")

        # Compile base config:
        sdlc_config.update({
            "applicationName": "Legend SDLC",
            "server": {
                "rootPath": APPLICATION_ROOT_PATH,
                "applicationConnectors": [{
                    "type": APPLICATION_CONNECTOR_TYPE_HTTP,
                    "port": APPLICATION_CONNECTOR_PORT_HTTP,
                    "maxRequestHeaderSize": "128KiB"
                }],
                "adminConnectors": [{
                    "type": APPLICATION_CONNECTOR_TYPE_HTTP,
                    "port": APPLICATION_ADMIN_CONNECTOR_PORT_HTTP
                }],
                "gzip": {
                    "includedMethods": ["GET", "POST"]
                },
                "requestLog": {
                    "type": "classic",
                    "level": request_logging_level,
                    "appenders": [{
                        "type": "console",
                        # TODO(aznashwan): check whether this will lead to
                        # senseless "OFF" lines in the logs:
                        "logFormat": "OFF"
                    }]
                }
            },
            "filterPriorities": {
                "GitLab": 1,
                "org.pac4j.j2e.filter.CallbackFilter": 2,
                "org.pac4j.j2e.filter.SecurityFilter": 3,
                "CORS": 4
            },
            "pac4j": {
                "callbackPrefix": "/api/pac4j/login",
                "mongoUri": mongo_creds['uri'],
                # TODO(aznashwan): must be set to the correct one:
                "mongoDb": mongo_creds['database'],
                "clients": [{
                    "org.finos.legend.server.pac4j.gitlab.GitlabClient": {
                        "name": "gitlab",
                        # TODO(aznashwan): set these on Gitlab relation:
                        "clientId": gitlab_client_id,
                        "secret": gitlab_client_secret,
                        # TODO(aznashwan): set this on gitlab rel too:
                        "discoveryUri": gitlab_openid_discovery_url,
                        # NOTE(aznashwan): needs to be a space-separated str:
                        "scope": " ".join(GITLAB_REQUIRED_SCOPES)
                    }
                }],
                "mongoSession": {
                    "enabled": True,
                    "collection": "userSessions"},
                "bypassPaths": ["/api/info"]
            },
            "gitLab": {
                "newProjectVisibility": gitlab_project_visibility,
                "projectTag": gitlab_project_tag,
                "uat": {
                    "server": {
                        # TODO(aznashwan): check if a 'port' is available:
                        "scheme": legend_gitlab_creds['gitlab_scheme'],
                        "host": legend_gitlab_creds['gitlab_host'],
                    },
                    "app": {
                        # TODO(aznashwan): set these on Gitlab relation:
                        "id": gitlab_client_id,
                        "secret": gitlab_client_secret,
                        "redirectURI": (
                            SDLC_MAIN_GITLAB_REDIRECT_URL % {
                                "base_url": self._get_sdlc_service_url()})
                    }
                },
            },
            "projectStructure": {
                "projectCreation": {
                    "groupIdPattern": gitlab_project_creation_group_pattern
                },
                "extensionProvider": {
                    "org.finos.legend.sdlc.server.gitlab.finos."
                    "FinosGitlabProjectStructureExtensionProvider": {}
                }
            },
            "logging": {
                "level": server_logging_level,
                "appenders": [{
                    "type": "console",
                    "logFormat": APPLICATION_LOGGING_FORMAT,
                }]
            },
            "swagger": {
                "title": "Legend SDLC",
                "resourcePackage": "org.finos.legend.sdlc.server.resources",
                "version": "local-snapshot",
                "schemes": []
            }
        })

        return None

    def _update_sdlc_service_config(
            self, container: model.Container, config: dict) -> None:
        """Generates YAML config for the SDLC server and adds it into the
        container via Pebble files API.
        """
        logger.debug(
            "Adding following config under '%s' in container: %s",
            SDLC_CONFIG_FILE_CONTAINER_LOCAL_PATH, config)
        container.push(
            SDLC_CONFIG_FILE_CONTAINER_LOCAL_PATH,
            yaml.dump(config),
            make_dirs=True)
        logger.info(
            "Successfully wrote config file in container under '%s'",
            SDLC_CONFIG_FILE_CONTAINER_LOCAL_PATH)

    def _restart_sdlc_service(self, container: model.Container) -> None:
        """Restarts the SDLC service using the Pebble container API.
        """
        logger.debug("Restarting SDLC service")
        container.restart("sdlc")
        logger.debug("Successfully issued SDLC service restart")

    def _write_java_truststore_to_container(self, container):
        """Creates a Java jsk truststore from the certificate in the GitLab
        relation data and adds it into the container under the appropriate
        path.
        Returns a `model.BlockedStatus` if any issue occurs.
        """
        gitlab_cert_b64 = self._stored.legend_gitlab_credentials.get(
            "gitlab_host_cert_b64")
        if not gitlab_cert_b64:
            return model.BlockedStatus(
                "no 'gitlab_host_cert_b64' present in relation data")

        gitlab_cert_raw = None
        try:
            gitlab_cert_raw = base64.b64decode(gitlab_cert_b64)
        except Exception as ex:
            logger.exception(ex)
            return model.BlockedStatus("failed to decode b64 cert")

        keystore_dump = None
        try:
            cert_entry = jks.TrustedCertEntry.new(
                TRUSTSTORE_NAME, gitlab_cert_raw)
            keystore = jks.KeyStore.new(
                TRUSTSTORE_TYPE_JKS, [cert_entry])
            keystore_dump = keystore.saves(TRUSTSTORE_PASSPHRASE)
        except Exception as ex:
            logger.exception(ex)
            return model.BlockedStatus(
                "failed to create jks keystore: %s", str(ex))

        logger.debug(
            "Adding jks trustore under '%s' in container",
            TRUSTSTORE_CONTAINER_LOCAL_PATH)
        container.push(
            TRUSTSTORE_CONTAINER_LOCAL_PATH,
            keystore_dump,
            make_dirs=True)
        logger.info(
            "Successfully wrote java truststore file to %s",
            TRUSTSTORE_CONTAINER_LOCAL_PATH)

    def _reconfigure_sdlc_service(self) -> None:
        """Generates the YAML config for the SDLC server and adds it into the
        container via Pebble files API.
        - regenerating the YAML config for the SDLC server
        - adding it via Pebble
        - instructing Pebble to restart the SDLC server
        The Service is power-cycled for the new configuration to take effect.
        """
        config = {}
        possible_blocked_status = (
            self._add_base_service_config_from_charm_config(config))
        if possible_blocked_status:
            logger.warning("Missing/erroneous configuration options")
            self.unit.status = possible_blocked_status
            return

        container = self.unit.get_container("sdlc")
        if container.can_connect():
            possible_blocked_status = (
                self._write_java_truststore_to_container(
                    container))
            if possible_blocked_status:
                self.unit.status = possible_blocked_status
                return

            logger.debug("Updating SDLC service configuration")
            self._update_sdlc_service_config(container, config)
            self._restart_sdlc_service(container)
            self.unit.status = model.ActiveStatus()
            return

        logger.info("SDLC container is not active yet. No config to update.")
        self.unit.status = model.BlockedStatus(
            "requires relating to: finos-legend-db-k8s, "
            "finos-legend-gitlab-integrator-k8s")

    def _on_config_changed(self, _) -> None:
        """Reacts to configuration changes to the service by:
        - regenerating the YAML config for the SDLC server
        - adding it via Pebble
        - instructing Pebble to restart the SDLC server
        """
        self._reconfigure_sdlc_service()

    def _on_db_relation_joined(self, event: charm.RelationJoinedEvent):
        logger.debug("No actions are to be performed during DB relation join")

    def _on_db_relation_changed(
            self, event: charm.RelationChangedEvent) -> None:
        mongo_creds = self._legend_db_consumer.get_legend_database_creds(
            event.relation.id)
        if not mongo_creds:
            self.unit.status = model.WaitingStatus(
                "awaiting legend db relation data")
            return
        logger.debug(
            "Mongo credentials returned by DB relation: %s",
            mongo_creds)
        self._stored.legend_db_credentials = mongo_creds

        # Attempt to reconfigure and restart the service with the new data:
        self._reconfigure_sdlc_service()

    def _get_sdlc_service_url(self):
        ip_address = subprocess.check_output(
            ["unit-get", "private-address"]).decode().strip()
        return SDLC_SERVICE_URL_FORMAT % ({
            # NOTE(aznashwan): we always return the plain HTTP endpoint:
            "schema": "http",
            "host": ip_address,
            "port": APPLICATION_CONNECTOR_PORT_HTTP,
            "path": APPLICATION_ROOT_PATH})

    def _on_studio_relation_joined(
            self, event: charm.RelationJoinedEvent) -> None:
        rel = event.relation
        sdlc_url = self._get_sdlc_service_url()
        logger.info("### Providing following SDLC URL to Studio: %s", sdlc_url)
        rel.data[self.app]["legend-sdlc-url"] = sdlc_url

    def _on_studio_relation_changed(
            self, event: charm.RelationChangedEvent) -> None:
        pass

    def _on_legend_gitlab_relation_joined(
            self, event: charm.RelationJoinedEvent) -> None:
        base_url = self._get_sdlc_service_url()
        redirect_uris = [
            fmt % {"base_url": base_url}
            for fmt in SDLC_GITLAB_REDIRECT_URI_FORMATS]

        legend_gitlab.set_legend_gitlab_redirect_uris_in_relation_data(
            event.relation.data[self.app], redirect_uris)

    def _on_legend_gitlab_relation_changed(
            self, event: charm.RelationChangedEvent) -> None:
        gitlab_creds = None
        try:
            gitlab_creds = (
                self._legend_gitlab_consumer.get_legend_gitlab_creds(
                    event.relation.id))
        except Exception as ex:
            logger.exception(ex)
            self.unit.status = model.BlockedStatus(
                "failed to retrieve GitLab creds from relation data, "
                "ensure finos-legend-gitlab-integrator-k8s is compatible")
            return

        if not gitlab_creds:
            self.unit.status = model.WaitingStatus(
                "awaiting legend gitlab credentials from integrator")
            event.defer()
            return

        self._stored.legend_gitlab_credentials = gitlab_creds
        self._reconfigure_sdlc_service()


if __name__ == "__main__":
    main.main(LegendSDLCServerCharm)
