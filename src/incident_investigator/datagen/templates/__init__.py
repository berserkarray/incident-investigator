"""Fault_Template registry. Templates are added per fault kind; the catalogue names one per scenario."""

from ...models.scenario import FaultTemplateName
from .base import FaultResult, FaultTemplate
from .cache_stampede import CacheStampede
from .cascading_failure import CascadingFailure
from .certificate_expiry import CertificateExpiry
from .config_regression import ConfigRegression
from .connection_pool_exhaustion import ConnectionPoolExhaustion
from .database_failover import DatabaseFailover
from .disk_full import DiskFull
from .feature_flag_regression import FeatureFlagRegression
from .lb_misconfiguration import LBMisconfiguration
from .memory_leak import MemoryLeak
from .symptoms_only import SymptomsOnly
from .third_party_outage import ThirdPartyOutage

TEMPLATES: dict[FaultTemplateName, FaultTemplate] = {
    template.name: template
    for template in (
        ConfigRegression(),
        DatabaseFailover(),
        ThirdPartyOutage(),
        MemoryLeak(),
        CertificateExpiry(),
        CacheStampede(),
        LBMisconfiguration(),
        DiskFull(),
        ConnectionPoolExhaustion(),
        SymptomsOnly(),
        FeatureFlagRegression(),
        CascadingFailure(),
    )
}

__all__ = ["TEMPLATES", "FaultResult", "FaultTemplate"]
