import bloop.client
import bloop.condition
import bloop.exceptions
import bloop.filter
import bloop.index
import bloop.model
import bloop.tracking
import bloop.util
import collections
import collections.abc
import declare

__all__ = ["Engine"]

MISSING = object()
DEFAULT_CONFIG = {
    "atomic": False,
    "consistent": False,
    "strict": True
}


def set_of(objs):
    """wrap single elements in a set"""
    if isinstance(objs, str):  # pragma: no cover
        return {objs}
    elif isinstance(objs, collections.abc.Iterable):
        return set(objs)
    else:
        return {objs}


def value_of(column):
    """value_of({'S': 'Space Invaders'}) -> 'Space Invaders'"""
    return next(iter(column.values()))


def index_for(key):
    """index_for({'id': {'S': 'foo'}, 'range': {'S': 'bar'}}) -> ('bar', 'foo')"""
    return tuple(sorted(value_of(k) for k in key.values()))


def extract_key(key_shape, item):
    """construct a key according to key_shape for building an index"""
    return {field: item[field] for field in key_shape}


def dump_key(engine, obj):
    """dump the hash (and range, if there is one) key(s) of an object into
    a dynamo-friendly format.

    returns {dynamo_name: {type: value} for dynamo_name in hash/range keys}
    """
    meta = obj.Meta
    hash_key, range_key = meta.hash_key, meta.range_key
    hash_value = getattr(obj, hash_key.model_name, MISSING)
    if hash_value is MISSING:
        raise ValueError("Must specify a value for the hash attribute '{}'".format(hash_key.model_name))
    key = {hash_key.dynamo_name: engine._dump(hash_key.typedef, hash_value)}
    if range_key:
        range_value = getattr(obj, range_key.model_name, MISSING)
        if range_value is MISSING:
            raise ValueError("Must specify a value for the range attribute '{}'".format(range_key.model_name))
        key[range_key.dynamo_name] = engine._dump(range_key.typedef, range_value)
    return key


def config(engine, key, value):
    """Return a given config value unless it's None.

    In that case, fall back to the engine's config value.
    """
    if value is None:
        return engine.config[key]
    return value


class Engine:
    client = None

    def __init__(self, client=None, type_engine=None, **config):
        # Unique namespace so the type engine for multiple bloop Engines
        # won't have the same TypeDefinitions
        self.type_engine = type_engine or declare.TypeEngine.unique()

        self.client = client
        self.config = dict(DEFAULT_CONFIG)
        self.config.update(config)

    def _dump(self, model, obj, context=None, **kwargs):
        """Return a dict of the obj in DynamoDB format"""
        try:
            context = context or {"engine": self}
            return context["engine"].type_engine.dump(model, obj, context=context, **kwargs)
        except declare.DeclareException:
            # Best-effort check for a more helpful message
            if isinstance(model, bloop.model.ModelMetaclass):
                raise bloop.exceptions.UnboundModel("load", model, obj)
            else:
                raise ValueError("Failed to dump unknown model {}".format(model))

    def _instance(self, model):
        """Return an instance of a given model"""
        return self._load(model, {})

    def _load(self, model, value, context=None, **kwargs):
        try:
            context = context or {"engine": self}
            return context["engine"].type_engine.load(model, value, context=context, **kwargs)
        except declare.DeclareException:
            # Best-effort check for a more helpful message
            if isinstance(model, bloop.model.ModelMetaclass):
                raise bloop.exceptions.UnboundModel("load", model, None)
            else:
                raise ValueError("Failed to load unknown model {}".format(model))

    def _update(self, obj, attrs, expected, context=None, **kwargs):
        """Push values by dynamo_name into an object"""
        context = context or {"engine": self}
        for column in expected:
            value = attrs.get(column.dynamo_name, None)
            if value is not None:
                value = context["engine"]._load(column.typedef, value, context=context, **kwargs)
            setattr(obj, column.model_name, value)

    def bind(self, *, base):
        """Create tables for all models subclassing base"""
        # If not manually configured, use a default bloop.Client
        # with the default boto3.client("dynamodb")
        self.client = self.client or bloop.client.Client()

        # Make sure we're looking at models
        if not isinstance(base, bloop.model.ModelMetaclass):
            raise ValueError("base must derive from bloop.new_base()")

        # whether the model's typedefs should be registered, and
        # whether the model should be eligible for validation
        def is_concrete(model):
            # Models that aren't explicitly abstract should be bound
            abstract = model.Meta.abstract
            return not abstract

        # whether the model needs to have create/validate calls made for its
        # backing table
        def is_verified(model):
            return bloop.tracking.is_model_verified(model)
        concrete = set(filter(is_concrete, bloop.util.walk_subclasses(base)))
        unverified = concrete - set(filter(is_verified, concrete))

        # create_table doesn't block until ACTIVE or validate.
        # It also doesn't throw when the table already exists, making it safe
        # to call multiple times for the same unbound model.
        for model in unverified:
            self.client.create_table(model)

        for model in concrete:
            if model in unverified:
                self.client.validate_table(model)
            # Model won't need to be verified the
            # next time its BaseModel is bound to an engine
            bloop.tracking.verify_model(model)

            self.type_engine.register(model)
            for column in model.Meta.columns:
                self.type_engine.register(column.typedef)
            self.type_engine.bind(context={"engine": self})

    def delete(self, objs, *, condition=None, atomic=None):
        objs = set_of(objs)
        for obj in objs:
            if obj.Meta.abstract:
                raise bloop.exceptions.AbstractModelException(obj)
        for obj in objs:
            item = {"TableName": obj.Meta.table_name, "Key": dump_key(self, obj)}
            renderer = bloop.condition.ConditionRenderer(self)

            item_condition = bloop.condition.Condition()
            if config(self, "atomic", atomic):
                item_condition &= bloop.tracking.get_snapshot(obj)
            if condition:
                item_condition &= condition
            renderer.render(item_condition, "condition")
            item.update(renderer.rendered)

            self.client.delete_item(item)
            bloop.tracking.clear(obj)

    def load(self, objs, consistent=None):
        """Populate objects from dynamodb, optionally using consistent reads.

        If any objects are not found, raises NotModified with the attribute
        `objects` containing a list of the objects that were not loaded.

        Example
        -------
        class HashOnly(bloop.new_base()):
            user_id = Column(NumberType, hash_key=True)

        class HashAndRange(bloop.new_base()):
            user_id = Column(NumberType, hash_key=True)
            game_title = Column(StringType, range_key=True)

        hash_only = HashOnly(user_id=101)
        hash_and_range = HashAndRange(user_id=101, game_title="Starship X")

        # Load only one instance, with consistent reads
        engine.load(hash_only, consistent=True)

        # Load multiple instances
        engine.load([hash_only, hash_and_range])
        """
        # For an in-depth breakdown of the loading algorithm,
        # see docs/dev/internal.rst::Loading
        consistent = config(self, "consistent", consistent)
        objs = set_of(objs)
        for obj in objs:
            if obj.Meta.abstract:
                raise bloop.exceptions.AbstractModelException(obj)

        table_index, object_index, request = {}, {}, {}

        for obj in objs:
            table_name = obj.Meta.table_name
            key = dump_key(self, obj)
            index = index_for(key)

            if table_name not in object_index:
                table_index[table_name] = list(sorted(key.keys()))
                object_index[table_name] = {}
                request[table_name] = {"Keys": [], "ConsistentRead": consistent}

            if index not in object_index[table_name]:
                request[table_name]["Keys"].append(key)
                object_index[table_name][index] = set()
            object_index[table_name][index].add(obj)

        response = self.client.batch_get_items(request)

        for table_name, blobs in response.items():
            for blob in blobs:
                key_shape = table_index[table_name]
                key = extract_key(key_shape, blob)
                index = index_for(key)

                for obj in object_index[table_name].pop(index):
                    self._update(obj, blob, obj.Meta.columns)
                    bloop.tracking.sync(obj, self)
                if not object_index[table_name]:
                    object_index.pop(table_name)

        if object_index:
            not_loaded = set()
            for index in object_index.values():
                for index_set in index.values():
                    not_loaded.update(index_set)
            raise bloop.exceptions.NotModified("load", not_loaded)

    def query(self, obj, consistent=None):
        if isinstance(obj, bloop.index._Index):
            model, index = obj.model, obj
            select = "projected"
        else:
            model, index = obj, None
            select = "all"
        if model.Meta.abstract:
            raise bloop.exceptions.AbstractModelException(model)

        return bloop.filter.Filter(
            engine=self, mode="query", model=model, index=index, strict=self.config["strict"], select=select,
            consistent=config(self, "consistent", consistent))

    def save(self, objs, *, condition=None, atomic=None):
        objs = set_of(objs)
        for obj in objs:
            if obj.Meta.abstract:
                raise bloop.exceptions.AbstractModelException(obj)
        for obj in objs:
            item = {"TableName": obj.Meta.table_name, "Key": dump_key(self, obj)}
            renderer = bloop.condition.ConditionRenderer(self)

            renderer.update_for(obj)

            item_condition = bloop.condition.Condition()
            if config(self, "atomic", atomic):
                item_condition &= bloop.tracking.get_snapshot(obj)
            if condition:
                item_condition &= condition
            renderer.render(item_condition, "condition")
            item.update(renderer.rendered)

            self.client.update_item(item)
            bloop.tracking.sync(obj, self)

    def scan(self, obj, consistent=None):
        if isinstance(obj, bloop.index._Index):
            model, index = obj.model, obj
            select = "projected"
        else:
            model, index = obj, None
            select = "all"
        if model.Meta.abstract:
            raise bloop.exceptions.AbstractModelException(model)
        return bloop.filter.Filter(
            engine=self, mode="query", model=model, index=index, strict=self.config["strict"], select=select,
            consistent=config(self, "consistent", consistent))
