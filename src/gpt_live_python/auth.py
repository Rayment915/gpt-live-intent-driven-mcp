import os

from azure.core.credentials import TokenCredential
from azure.identity import AzureCliCredential, DefaultAzureCredential, ManagedIdentityCredential

from .config import Settings


def credential(config: Settings) -> TokenCredential:
    if os.getenv("CONTAINER_APP_NAME"):
        client = os.getenv("AZURE_CLIENT_ID")
        return ManagedIdentityCredential(client_id=client) if client else ManagedIdentityCredential()
    if config.azure_subscription_id and not os.getenv("WEBSITE_SITE_NAME"):
        return AzureCliCredential(subscription=config.azure_subscription_id)
    return DefaultAzureCredential(exclude_interactive_browser_credential=True)
