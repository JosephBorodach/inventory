import asyncio

from viam.components.generic import Generic
from viam.module.module import Module
from viam.resource.registry import Registry, ResourceCreatorRegistration

from .tracker import Tracker


def _register() -> None:
    Registry.register_resource_creator(
        Generic.API,
        Tracker.MODEL,
        ResourceCreatorRegistration(Tracker.new, Tracker.validate_config),
    )


async def main() -> None:
    _register()
    module = Module.from_args()
    module.add_model_from_registry(Generic.API, Tracker.MODEL)
    await module.start()


if __name__ == "__main__":
    asyncio.run(main())
