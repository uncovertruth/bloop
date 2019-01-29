# coding: utf-8


class BaseOperation:

    operation = None

    def __init__(self, value):

        if self.operation is None:
            raise Exception('required self.operation')

        self.value = value


class Add(BaseOperation):
    operation = 'ADD'


class ExpressionFunction:

    def __init__(self, name, value):
        self.name = name
        self.name = value

    def __repr__(self):
        return "{}(name={}, value={})".format(self.__class__.__name__, self.name, self.value)
