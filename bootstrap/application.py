from pathlib import Path

from fastapi_startkit import Application
from fastapi_startkit.inertia import InertiaProvider
from fastapi_startkit.logging import LogProvider
from fastapi_startkit.vite import ViteProvider

from config.app import AppConfig
from config.fastapi import FastAPIConfig
from config.logging import LoggingConfig
from config.tortoise import TortoiseConfig
from config.vite import ViteConfig
from providers.fastapi_provider import FastapiProvider
from providers.tortoise_provider import TortoiseProvider
from config.queue import QueueConfig
from providers.queue_provider import QueueProvider

app = Application(
    base_path=Path(__file__).resolve().parent.parent,
    config=AppConfig,
    providers=[
        (LogProvider, LoggingConfig),
        (TortoiseProvider, TortoiseConfig),
        (FastapiProvider, FastAPIConfig),
        (ViteProvider, ViteConfig),
        InertiaProvider,
        (QueueProvider, QueueConfig),
    ],
)
