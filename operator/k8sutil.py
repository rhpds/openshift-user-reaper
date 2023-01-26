import asyncio
import os

import kubernetes_asyncio

from inflection import singularize

class K8sUtil:
    @classmethod
    async def on_startup(cls):
        if os.path.exists('/run/secrets/kubernetes.io/serviceaccount'):
            kubernetes_asyncio.config.load_incluster_config()
        else:
            await kubernetes_asyncio.config.load_kube_config()

        cls.core_v1_api = kubernetes_asyncio.client.CoreV1Api()
        cls.custom_objects_api = kubernetes_asyncio.client.CustomObjectsApi()

    @classmethod
    async def list_objects(cls, plural, _continue=None, group=None, limit=None, namespace=None, version='v1'):
        if group:
            kwargs = dict(
                _continue = _continue,
                group = group,
                limit = limit,
                plural = plural,
                version = version,
            )
            if namespace:
                return await cls.custom_objects_api.list_namespaced_custom_object(
                    namespace = namespace,
                    **kwargs
                )
            else:
                return await cls.custom_objects_api.list_cluster_custom_object(**kwargs)
        else:
            kwargs = dict(_continue=_continue, limit=limit)
            if namespace:
                method = getattr(cls.core_v1_api, "list_namespaced_" + singularize(plural))
                kwargs['namespace'] = namespace
            else:
                method = "list_" + singularize(plural)
            return await method(**kwargs)


class K8sObject:
    @classmethod
    def cache_load(cls, definition):
        cache_key = cls.cache_key_from_definition(definition)
        obj = cls.cache.get(cache_key)
        if obj:
            obj.definition = definition
        else:
            obj = cls(definition)
            cls.cache[cache_key] = obj
        return obj

    def __init__(self, definition):
        self.definition = definition
        self.lock = asyncio.Lock()

    @property
    def annotations(self):
        return self.metadata.get('annotations', {})

    @property
    def creation_timestamp(self):
        return self.metadata['creationTimestamp']

    @property
    def labels(self):
        return self.metadata.get('labels', {})

    @property
    def metadata(self):
        return self.definition['metadata']

    @property
    def name(self):
        return self.metadata['name']

    @property
    def uid(self):
        return self.metadata['uid']

class K8sClusterObject(K8sObject):
    @classmethod
    def cache_key_from_definition(cls, definition):
        return definition['metadata']['name']

    @classmethod
    async def get(cls, name):
        definition = await K8sUtil.custom_objects_api.get_cluster_custom_object(
            group = cls.group,
            name = name,
            plural = cls.plural,
            version = cls.version,
        )
        return cls(definition)

    def __str__(self):
        return f"{self.kind} {self.name}"

    async def delete(self):
        try:
            await K8sUtil.custom_objects_api.delete_cluster_custom_object(
                group = self.group,
                name = self.name,
                plural = self.plural,
                version = self.version,
            )
        except kubernetes_asyncio.client.rest.ApiException as e:
            if e.status != 404:
                raise

    async def merge_patch(self, patch):
        definition = await K8sUtil.custom_objects_api.patch_cluster_custom_object(
            group = self.group,
            name = self.name,
            plural = self.plural,
            version = self.version,
            body = patch,
            _content_type = 'application/merge-patch+json',
        )
        self.definition = definition
