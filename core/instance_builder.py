"""Assemble isolated instance records from resolved plans without starting them.

Resolved dependency factories remain references until lifecycle and provider
contracts are implemented. This builder does not install software, invoke
simulators, start observers or claim a healthy deployment.
"""

from dataclasses import dataclass, field
from typing import Any

from core.dependency_resolver import BuildPlan, InstancePlan


class InstanceBuildError(ValueError):
    """A resolved build plan could not be assembled consistently."""


@dataclass
class FrameworkInstance:
    plan: InstancePlan = field(repr=False)
    state: dict[str, Any] = field(default_factory=lambda: {
        "phase": "planned", "ready": False,
    })
    resources: dict[str, Any] = field(default_factory=dict, repr=False)
    observations: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def instance_id(self) -> str:
        return self.plan.instance_id

    @property
    def deployment(self) -> str:
        return self.plan.config["deployment"]

    @property
    def owns_platform_resources(self) -> bool:
        # Managed mode permits future ownership; actual ownership requires launch.
        return (self.deployment == "managed"
                and self.resources.get("owned_platform_resources") is True)


def build_instances(plan: BuildPlan) -> dict[str, FrameworkInstance]:
    """Create per-instance state and resource containers from a resolved plan.

    Fresh containers are allocated on every call. Both managed and attached
    instances start with no owned resources. This checks IDs and ports again to
    reject inconsistent plans before creating any records.
    """
    if not isinstance(plan, BuildPlan):
        raise InstanceBuildError("Expected a resolved BuildPlan")
    result = {}
    used_ports = set()
    adapter_objects = set()
    for item in plan.instances:
        if not isinstance(item, InstancePlan) or item.instance_id != item.config.get("id"):
            raise InstanceBuildError("Plan instance identity is inconsistent")
        if item.instance_id in result:
            raise InstanceBuildError("Duplicate instance ID in build plan")
        if id(item.adapter) in adapter_objects:
            raise InstanceBuildError("Adapters must be isolated per instance")
        adapter_objects.add(id(item.adapter))
        for port in item.config["services"].values():
            if port in used_ports:
                raise InstanceBuildError("Duplicate service port in build plan")
            used_ports.add(port)
        result[item.instance_id] = FrameworkInstance(item)
    return result
