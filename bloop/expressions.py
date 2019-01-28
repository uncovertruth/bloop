# coding: utf-8


class BaseOperation:
    def __init__(self, value):
        self.value = value


class Add(BaseOperation):
    pass


class ExpressionFunction:

    def __init__(self, name, value):
        self.name = name
        self.name = value

    def __repr__(self):
        return "{}(name={}, value={})".format(self.__class__.__name__, self.name, self.value)
