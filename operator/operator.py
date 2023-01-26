import asyncio
import kopf
import logging
import os
import yaml

from datetime import datetime, timedelta, timezone

import kubernetes_asyncio

from configure_kopf_logging import configure_kopf_logging
from infinite_relative_backoff import InfiniteRelativeBackoff
from k8sutil import K8sClusterObject, K8sUtil

app_domain = os.environ.get('APP_DOMAIN', 'demo.redhat.com')
last_login_annotation = os.environ.get('LAST_LOGIN_ANNOTATION', f"{app_domain}/last-login")
namespace_check_resources = yaml.safe_load(os.environ.get('NAMESPACE_CHECK_RESOURCES', '[]'))
days_after_last_login = int(os.environ.get('DAYS_AFTER_LAST_LOGIN', 30))

class Namespace(K8sClusterObject):
    cache = {}
    kind = 'Namespace'

    @classmethod
    def namespaces_for_user(cls, user):
        for ns in cls.cache.values():
            if ns.requester == user.name:
                yield ns

    @property
    def requester(self):
        return self.annotations.get('openshift.io/requester')
    

class User(K8sClusterObject):
    cache = {}
    group = 'user.openshift.io'
    kind = 'User'
    plural = 'users'
    version = 'v1'

    @property
    def creation_timestamp(self):
        return self.metadata['creationTimestamp']

    @property
    def identities(self):
        return self.definition.get('identities', [])

    @property
    def last_login_timestamp(self):
        return self.annotations.get(last_login_annotation)

    @property
    def last_login_datetime(self):
        timestamp = self.last_login_timestamp
        return datetime.strptime(timestamp, '%Y-%m-%dT%H:%M:%S%z') if timestamp else None

    async def check_reap_user(self):
        logging.info(f"Checking reap for {self.name}")
        for ns in Namespace.namespaces_for_user(self):
            logging.debug(f"Checking {ns} for resources for {self.name}")
            for check_resource in namespace_check_resources:
                logging.info(f"Checking {ns} for {check_resource['plural']}")
                try:
                    ret = await K8sUtil.list_objects(
                        limit = 1,
                        namespace = ns.name,
                        **check_resource,
                    )
                    items = ret['items'] if isinstance(ret, dict) else ret.items
                    if items:
                        logging.info(f"Not reaping {self.name}, {check_resource['plural']} in {ns.name}")
                        return False
                except Exception as e:
                    logging.exception(f"Not reaping {self.name}, exception during check!")
                    return False

        logging.info(f"Reaping {self.name}")
        await self.delete()
        return True

    async def cancel_reap_task(self):
        if not hasattr(self, 'task'):
            return
        if not self.task.done():
            logging.debug(f"Canceling check_reap_user for {self}")
            self.task.cancel()
        await self.task

    async def delete(self):
        for identity in self.identities:
            await K8sUtil.custom_objects_api.delete_cluster_custom_object(
                group = 'user.openshift.io',
                name = identity,
                plural = 'identities',
                version = 'v1',
            )
        await super().delete()

    async def schedule_check_reap_user(self):
        try:
            last_login_datetime = self.last_login_datetime
            if not last_login_datetime:
                logging.info(f"Not checking {self}, no last login timestamp.")
                return

            reap_after_datetime = last_login_datetime + timedelta(days=days_after_last_login)
            # If reap after is in the past then adjust it to the future at 24 hour offset.
            if reap_after_datetime < datetime.now(timezone.utc):
                reap_after_datetime = reap_after_datetime + timedelta(
                    seconds = 86400 * (1 + (datetime.now(timezone.utc) - reap_after_datetime).total_seconds() // 86400)
                )

            logging.info(f"Waiting until {reap_after_datetime.strftime('%FT%TZ')} to check {self}")
            await asyncio.sleep((reap_after_datetime - datetime.now(timezone.utc)).total_seconds())

            while True:
                if await self.check_reap_user():
                    return
                # Sleep one day
                await asyncio.sleep(86400)

        except asyncio.CancelledError:
            logging.debug(f"Canceled scheduled check for {self}")

    async def start_reaper_task(self):
        await self.cancel_reap_task()
        self.task = asyncio.create_task(
            self.schedule_check_reap_user()
        )


@kopf.on.startup()
async def startup(settings: kopf.OperatorSettings, **_):
    # Never give up from network errors
    settings.networking.error_backoffs = InfiniteRelativeBackoff()

    # Only create events for warnings and errors
    settings.posting.level = logging.WARNING

    # Disable scanning for CustomResourceDefinitions updates
    settings.scanning.disabled = True

    # Configure logging
    configure_kopf_logging()

    await K8sUtil.on_startup()

    namespace_list = await K8sUtil.core_v1_api.list_namespace()
    for ns in namespace_list.items:
        if 'openshift.io/requester' in ns.metadata.annotations:
            Namespace.cache_load(
                K8sUtil.core_v1_api.api_client.sanitize_for_serialization(ns)
            )


@kopf.on.event('namespaces')
async def user_event(event, logger, **_):
    definition = event['object']
    name = definition['metadata']['name']
    if event['type'] == 'DELETED':
        Namespace.cache.pop(name, None)
        return

    if 'openshift.io/requester' not in definition['metadata'].get('annotations', {}):
        Namespace.cache.pop(name, None)
        return

    Namespace.cache_load(definition)


@kopf.on.event('user.openshift.io', 'v1', 'users')
async def user_event(event, logger, **_):
    if event['type'] == 'DELETED':
        user = User.cache.pop(event['object']['metadata']['name'], None)
        if user:
            await user.cancel_reap_task()
        return

    user = User.cache_load(event['object'])
    async with user.lock:
        await user.start_reaper_task()
