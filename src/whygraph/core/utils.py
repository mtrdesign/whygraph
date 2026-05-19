from typing import Any, Dict


class SingletonMeta(type):
    """
    The SingleTonMeta util for defining singleton classes.
    """

    _instances: Dict[Any, Any] = {}

    def __call__(cls, *args, **kwargs):
        """
        Possible changes to the value of the `__init__` argument do not affect
        the returned instance.
        """
        if cls not in cls._instances:
            instance = super().__call__(*args, **kwargs)
            cls._instances[cls] = instance

        return cls._instances[cls]
