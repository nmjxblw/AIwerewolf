"""狼人杀完整分支树迭代与精确信念决策矩阵模拟器。"""

from importlib import import_module

from ._game_state import GameState
from ._interval import RewardInterval
from ._interval import RobustIntervals
from ._positions import PositionLayout
from ._positions import enumerate_position_layouts
from ._simulator import SearchSimulator

_DECISION_MATRIX_EXPORTS = {
    "CanonicalGameConfig": ("._decision_state", "CanonicalGameConfig"),
    "DecisionState": ("._decision_state", "DecisionState"),
    "RoleView": ("._role_view", "RoleView"),
    "SpeechPlan": ("._speech_action", "SpeechPlan"),
    "RuleAction": ("._rule_kernel", "RuleAction"),
    "RuleKernel": ("._rule_kernel", "RuleKernel"),
    "TreeSearchCompatibilityAdapter": ("._rule_kernel", "TreeSearchCompatibilityAdapter"),
    "DecisionMatrixCalculator": ("._decision_matrix", "DecisionMatrixCalculator"),
    "DecisionMatrixCell": ("._decision_matrix", "DecisionMatrixCell"),
    "MatrixInterrupted": ("._decision_matrix", "MatrixInterrupted"),
    "DecisionMatrixRequest": ("._decision_matrix", "DecisionMatrixRequest"),
    "DecisionMatrixResult": ("._decision_matrix", "DecisionMatrixResult"),
    "build_default_decision_request": ("._decision_matrix", "build_default_decision_request"),
    "run_default_matrix": ("._decision_matrix", "run_default_matrix"),
    "DecisionMatrixInputError": ("._matrix_api", "DecisionMatrixInputError"),
    "build_custom_decision_matrix_request": ("._matrix_api", "build_custom_decision_matrix_request"),
    "calculate_custom_decision_matrix": ("._matrix_api", "calculate_custom_decision_matrix"),
    "load_custom_decision_matrix_cell": ("._matrix_api", "load_custom_decision_matrix_cell"),
}


def __getattr__(name: str):
    """按需加载决策矩阵导出，避免 spawn worker 包级导入数据库层。"""

    target = _DECISION_MATRIX_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute_name = target
    value = getattr(import_module(module_name, __name__), attribute_name)
    globals()[name] = value
    return value


__all__ = [
    "GameState",
    "PositionLayout",
    "RewardInterval",
    "RobustIntervals",
    "SearchSimulator",
    "enumerate_position_layouts",
    "CanonicalGameConfig",
    "DecisionState",
    "RoleView",
    "SpeechPlan",
    "RuleAction",
    "RuleKernel",
    "TreeSearchCompatibilityAdapter",
    "DecisionMatrixCalculator",
    "DecisionMatrixCell",
    "MatrixInterrupted",
    "DecisionMatrixRequest",
    "DecisionMatrixResult",
    "build_default_decision_request",
    "run_default_matrix",
    "DecisionMatrixInputError",
    "build_custom_decision_matrix_request",
    "calculate_custom_decision_matrix",
    "load_custom_decision_matrix_cell",
]
