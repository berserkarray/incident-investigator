"""Fault_Template registry. Templates are added per fault kind; the catalogue names one per scenario."""

from ...models.scenario import FaultTemplateName
from .base import FaultResult, FaultTemplate
from .config_regression import ConfigRegression

TEMPLATES: dict[FaultTemplateName, FaultTemplate] = {
    ConfigRegression.name: ConfigRegression(),
}

__all__ = ["TEMPLATES", "FaultResult", "FaultTemplate"]
