import collections.abc
import logging

from . import util
from .conditions import ComparisonMixin
from .exceptions import InvalidIndex, InvalidModel, InvalidStream
from .signals import model_created, object_modified
from .types import Type


__all__ = ["BaseModel", "Column", "GlobalSecondaryIndex", "LocalSecondaryIndex"]
logger = logging.getLogger("bloop.models")
missing = util.missing


def loaded_columns(obj):
    """Yields each (model_name, value) tuple for all columns in an object that aren't missing"""
    for column in sorted(obj.Meta.columns, key=lambda c: c.model_name):
        value = getattr(obj, column.model_name, missing)
        if value is not missing:
            yield column.model_name, value


def unpack_from_dynamodb(*, attrs, expected, model=None, obj=None, engine=None, context=None, **kwargs):
    """Push values by dynamo_name into an object"""
    context = context or {"engine": engine}
    engine = engine or context.get("engine", None)
    if not engine:
        raise ValueError("You must provide engine or a context with an engine.")
    if model is None and obj is None:
        raise ValueError("You must provide a model or obj to unpack.")
    if model is not None and obj is not None:
        raise ValueError("Only specify model or obj.")
    if model:
        obj = model.Meta.init()

    for column in expected:
        value = attrs.get(column.dynamo_name, None)
        value = engine._load(column.typedef, value, context=context, **kwargs)
        setattr(obj, column.model_name, value)
    return obj


def validate_projection(projection):
    validated_projection = {
        "mode": None,
        "included": None,
        "available": None,
        "strict": True
    }

    # String check first since it is also an Iterable.
    # Without this, the following will make "unknown" a list
    if isinstance(projection, str):
        if projection not in ("keys", "all"):
            raise InvalidIndex(f"{projection!r} is not a valid Index projection.")
        validated_projection["mode"] = projection
    elif isinstance(projection, collections.abc.Iterable):
        projection = list(projection)
        # These checks aren't done together; that would allow a mix
        # of column instances and column names.  There aren't any cases
        # where a mix is required, over picking a style.  Much more likely,
        # the user is trying to do something odd and doesn't understand what
        # the index projection means.
        if (
                all(isinstance(p, str) for p in projection) or
                all(isinstance(p, Column) for p in projection)):
            validated_projection["mode"] = "include"
            validated_projection["included"] = projection
        else:
            raise InvalidIndex("Index projection must be a list of strings or Columns to select specific Columns.")
    else:
        raise InvalidIndex("Index projection must be 'all', 'keys', or a list of Columns or Column names.")
    return validated_projection


def validate_stream(stream):
    if stream is None:
        return

    if not isinstance(stream, collections.abc.MutableMapping):
        raise InvalidStream("Stream must be None or a dict.")

    if "include" not in stream:
        raise InvalidStream("Specify what the stream will return with the 'include' key.")
    include = stream["include"] = set(stream["include"])

    # []
    if not include:
        raise InvalidStream("Must include at least one of 'keys', 'old', or 'new'.")

    # ["what is this", "keys"]
    for value in include:
        if value not in {"new", "keys", "old"}:
            raise InvalidStream("Streams can only contain 'keys', 'old', and/or 'new'.")

    # ["keys", "old"]
    if include == {"new", "keys"} or include == {"old", "keys"}:
        raise InvalidStream("The option 'keys' cannot be used with either 'old' or 'new'.")
    stream.setdefault("arn", None)


def setdefault(obj, field, default):
    """Set an object's field to default if it doesn't have a value"""
    setattr(obj, field, getattr(obj, field, default))


def ensure_meta(cls):
    meta = getattr(cls, "Meta", missing)
    # Meta can't be inherited, otherwise we are mutating
    # a shared BaseModel.Meta's table_name, write_units, etc.
    for base in cls.__mro__:
        # Should only be the first entry in __mro__
        if base is cls:
            continue
        parent_meta = getattr(base, "Meta", None)
        # We will always collide with BaseModel.Meta;
        # stop searching on the first collision and
        # clear meta so we create a new class below.
        if meta is parent_meta:
            meta = missing
            break
    if meta is missing:
        class Meta:
            pass
        meta = cls.Meta = Meta
    return meta


def ensure_hash(cls):
    if getattr(cls, "__hash__", None) is not None:
        return
    # Any base class's explicit (not object.__hash__)
    # hash function has priority over the default.
    # If there aren't any bases with explicit hash functions,
    # just use object.__hash__
    logger.info(f"searching for nearest __hash__ impl in {cls.__name__}.__mro__")
    hash_fn = object.__hash__
    for base in cls.__mro__:  # pragma: no branch (because __mro__ will never be an empty list)
        hash_fn = getattr(base, "__hash__")
        if hash_fn:
            break
    cls.__hash__ = hash_fn


def setup_columns(meta):
    """Filter columns from fields, identify hash and range keys"""

    # This is a set instead of a list, because set uses __hash__
    # while some list operations uses __eq__ which will break
    # with the ComparisonMixin
    meta.columns = set()
    for attr in meta.model.__dict__.values():
        if isinstance(attr, Column):
            meta.columns.add(attr)

    meta.hash_key = None
    meta.range_key = None
    meta.keys = set()

    if not meta.abstract:
        cls_name = meta.model.__name__

        hash_keys = [c for c in meta.columns if c.hash_key]
        range_keys = [c for c in meta.columns if c.range_key]

        if len(hash_keys) == 0:
            raise InvalidModel(f"{cls_name!r} has no hash key.")
        elif len(hash_keys) > 1:
            raise InvalidModel(f"{cls_name!r} has more than one hash key.")

        if len(range_keys) > 1:
            raise InvalidModel(f"{cls_name!r} has more than one range key.")

        if range_keys:
            if hash_keys[0] is range_keys[0]:
                raise InvalidModel(f"{cls_name!r} has the same hash and range key.")
            meta.range_key = range_keys[0]
            meta.keys.add(meta.range_key)
        meta.hash_key = hash_keys[0]
        meta.keys.add(meta.hash_key)

    # API consistency with an Index, so (index or model.Meta) can be
    # used interchangeably to get the available columns from that
    # object.
    meta.projection = {
        "mode": "all",
        "included": meta.columns,
        "available": meta.columns,
        "strict": True
    }


def setup_indexes(meta):
    """Filter indexes from fields, compute projection for each index"""
    # Don't put these in the metadata until they bind successfully.
    gsis = set()
    lsis = set()
    for attr in meta.model.__dict__.values():
        if isinstance(attr, GlobalSecondaryIndex):
            gsis.add(attr)
        if isinstance(attr, LocalSecondaryIndex):
            lsis.add(attr)
    meta.gsis = gsis
    meta.lsis = lsis
    meta.indexes = set.union(gsis, lsis)

    for index in meta.indexes:
        bind_index(meta, index)


def bind_index(meta, index):
    """Compute attributes and resolve column names.

    * If hash and/or range keys are strings, resolve them to :class:`~bloop.models.Column` instances from
      the model by ``model_name``.
    * If projection is a list of strings, resolve each to a Column instance.
    * Compute :data:`~Index.projection` dict from model Metadata and Index's temporary ``projection``
      attribute.

    :param meta: The Meta of the :class:`~bloop.models.BaseModel` this Index is attached to.
    :param index: The :class:`~bloop.models.Index` to attach to the model.
    :raises bloop.exceptions.InvalidIndex: If the hash or range keys are misconfigured.
    """
    # Index by model_name so we can replace hash_key, range_key with the proper `bloop.Column` object
    columns = util.index(meta.columns, "model_name")
    if isinstance(index, LocalSecondaryIndex):
        if not meta.range_key:
            raise InvalidIndex("An LSI requires the Model to have a range key.")
        index.hash_key = meta.hash_key
    if isinstance(index.hash_key, str):
        index.hash_key = columns[index.hash_key]
    if not isinstance(index.hash_key, Column):
        raise InvalidIndex("Index hash key must be a Column or Column model name.")
    if index.range_key:
        if isinstance(index.range_key, str):
            index.range_key = columns[index.range_key]
        if not isinstance(index.range_key, Column):
            raise InvalidIndex("Index range key (if provided) must be a Column or Column model name.")

    index.keys = {index.hash_key}
    if index.range_key:
        index.keys.add(index.range_key)

    # Compute and the projected columns
    # All projections include model + index keys
    projection_keys = set.union(meta.keys, index.keys)

    if index.projection["mode"] == "keys":
        index.projection["included"] = projection_keys
    elif index.projection["mode"] == "all":
        index.projection["included"] = meta.columns
    elif index.projection["mode"] == "include":  # pragma: no branch
        # model_name -> Column
        if all(isinstance(p, str) for p in index.projection["included"]):
            projection = set(columns[name] for name in index.projection["included"])
        else:
            projection = set(index.projection["included"])
        projection.update(projection_keys)
        index.projection["included"] = projection

    # Strict has the same availability as the included columns,
    # while non-strict has access to the full range of columns
    if index.projection["strict"]:
        index.projection["available"] = index.projection["included"]
    else:
        index.projection["available"] = meta.columns


class BaseModel:
    """Abstract base that all models derive from.

    Provides a basic ``__init__`` method that takes \*\*kwargs whose
    keys are columns names:

    .. code-block:: python

        class URL(BaseModel):
            id = Column(UUID, hash_key=True)
            ip = Column(IPv6)
            name = Column(String)

        url = URL(id=uuid.uuid4(), name="google")
    """
    class Meta:
        abstract = True

    def __init__(self, **attrs):
        # Only set values from **attrs if there's a
        # corresponding `model_name` for a column in the model
        for column in self.Meta.columns:
            value = attrs.get(column.model_name, missing)
            if value is not missing:
                setattr(self, column.model_name, value)

    def __init_subclass__(cls, **kwargs):
        ensure_hash(cls)
        meta = ensure_meta(cls)
        meta.model = cls

        # Entry point for model population. By default this is the
        # class's __init__ function. Custom models can specify the
        # Meta attr `init`, which must be a function taking no
        # arguments that returns an instance of the class
        setdefault(meta, "init", cls)

        setdefault(meta, "abstract", False)
        setdefault(meta, "table_name", cls.__name__)
        setdefault(meta, "write_units", None)
        setdefault(meta, "read_units", None)
        setdefault(meta, "stream", None)

        setup_columns(meta)
        setup_indexes(meta)

        validate_stream(meta.stream)
        model_created.send(None, model=cls)

    @classmethod
    def _load(cls, attrs, *, context, **kwargs):
        """ dict (dynamo name) -> obj """
        return unpack_from_dynamodb(
            model=cls,
            attrs=attrs or {},
            expected=cls.Meta.columns,
            context=context, **kwargs)

    @classmethod
    def _dump(cls, obj, *, context, **kwargs):
        """ obj -> dict """
        if obj is None:
            return None
        dump = context["engine"]._dump
        filtered = filter(
            lambda item: item[1] is not None,
            ((
                column.dynamo_name,
                dump(column.typedef, getattr(obj, column.model_name, None), context=context, **kwargs)
            ) for column in cls.Meta.columns))
        return dict(filtered) or None

    def __repr__(self):
        attrs = ", ".join("{}={!r}".format(*item) for item in loaded_columns(self))
        return f"{self.__class__.__name__}({attrs})"


class Index:
    """Abstract base class for GSIs and LSIs.

    An index must be bound to a model by calling :func:`bind_index(meta, model) <bloop.models.bind_index>`,
    which lets the index compute projected columns, validate hash and range keys, etc.

    .. seealso::

        :class:`~bloop.models.GlobalSecondaryIndex` and :class:`~bloop.models.LocalSecondaryIndex`

    :param projection: Either "keys", "all", or a list of column name or objects.
        Included columns will be projected into the index.  Key columns are always included.
    :param hash_key: The column that the index can be queried against.  Always the table hash_key for LSIs.
    :param range_key: The column that the index can be sorted on.  Always required for an LSI.  Default is None.
    :param str name: *(Optional)* The index's name in in DynamoDB. Defaults to the index’s name in the model.
    """
    def __init__(self, *, projection, hash_key=None, range_key=None, name=None, **kwargs):
        self.model = None
        self.hash_key = hash_key
        self.range_key = range_key
        self._model_name = None
        self._dynamo_name = name

        self.projection = validate_projection(projection)

    def __set_name__(self, owner, name):
        self.model = owner
        self._model_name = name

    def __repr__(self):
        if isinstance(self, LocalSecondaryIndex):
            cls_name = "LSI"
        elif isinstance(self, GlobalSecondaryIndex):
            cls_name = "GSI"
        else:
            cls_name = self.__class__.__name__

        # <GSI[User.by_email=all]>
        # <GSI[User.by_email=keys]>
        # <LSI[User.by_email=include]>
        return f"<{cls_name}[{self.model.__name__}.{self.model_name}={self.projection['mode']}]>"

    @property
    def model_name(self):
        """Name of the model's attr that references self"""
        return self._model_name

    @property
    def dynamo_name(self):
        if self._dynamo_name is None:
            return self.model_name
        return self._dynamo_name

    def __set__(self, obj, value):
        raise AttributeError(f"{self.model.__name__}.{self.model_name} is a {self.__class__.__name__}")

    def __get__(self, obj, type=None):
        if obj is None:
            return self
        raise AttributeError(f"{self.model.__name__}.{self.model_name} is a {self.__class__.__name__}")

    def __delete__(self, obj):
        raise AttributeError(f"{self.model.__name__}.{self.model_name} is a {self.__class__.__name__}")


class GlobalSecondaryIndex(Index):
    """See `GlobalSecondaryIndex`_ in the DynamoDB Developer Guide for details.

    :param projection: Either "keys", "all", or a list of column name or objects.
        Included columns will be projected into the index.  Key columns are always included.
    :param hash_key: The column that the index can be queried against.
    :param range_key: *(Optional)* The column that the index can be sorted on.  Default is None.
    :param int read_units: *(Optional)* Provisioned read units for the index.  Default is None.
        When no value is provided and the index does not exist, it will be created with 1 read unit.  If the index
        already exists, it will use the actual index's read units.
    :param int write_units:  *(Optional)* Provisioned write units for the index.  Default is None.
        When no value is provided and the index does not exist, it will be created with 1 write unit.  If the index
        already exists, it will use the actual index's write units.
    :param str name: *(Optional)* The index's name in in DynamoDB. Defaults to the index’s name in the model.

    .. _GlobalSecondaryIndex: http://docs.aws.amazon.com/amazondynamodb/latest/developerguide/GSI.html
    """
    def __init__(
            self, *, projection,
            hash_key, range_key=None,
            read_units=None, write_units=None,
            name=None, **kwargs):
        super().__init__(hash_key=hash_key, range_key=range_key, name=name, projection=projection, **kwargs)
        self.write_units = write_units
        self.read_units = read_units


class LocalSecondaryIndex(Index):
    """See `LocalSecondaryIndex`_ in the DynamoDB Developer GUide for details.

    Unlike :class:`~bloop.models.GlobalSecondaryIndex`\, LSIs share their throughput with the table,
    and their hash key is always the table hash key.

    :param projection: Either "keys", "all", or a list of column name or objects.
        Included columns will be projected into the index.  Key columns are always included.
    :param range_key: The column that the index can be sorted against.
    :param str name: *(Optional)* The index's name in in DynamoDB. Defaults to the index’s name in the model.
    :param bool strict: *(Optional)* Restricts queries and scans on the LSI to columns in the projection.
        When False, DynamoDB may silently incur additional reads to load results.  You should not disable this
        unless you have an explicit need.  Default is True.

    .. _LocalSecondaryIndex: http://docs.aws.amazon.com/amazondynamodb/latest/developerguide/LSI.html
    """
    def __init__(self, *, projection, range_key, name=None, strict=True, **kwargs):
        # Hash key MUST be the table hash; do not specify
        if "hash_key" in kwargs:
            raise InvalidIndex("An LSI shares its hash key with the Model.")
        if ("write_units" in kwargs) or ("read_units" in kwargs):
            raise InvalidIndex("An LSI shares its provisioned throughput with the Model.")
        super().__init__(range_key=range_key, name=name, projection=projection, **kwargs)
        self.projection["strict"] = strict

    @property
    def read_units(self):
        return self.model.Meta.read_units

    @read_units.setter
    def read_units(self, value):
        self.model.Meta.read_units = value

    @property
    def write_units(self):
        return self.model.Meta.write_units

    @write_units.setter
    def write_units(self, value):
        self.model.Meta.write_units = value


def subclassof(obj, classinfo):
    """Wrap issubclass to only return True/False"""
    try:
        return issubclass(obj, classinfo)
    except TypeError:
        return False


def instanceof(obj, classinfo):
    """Wrap isinstance to only return True/False"""
    try:
        return isinstance(obj, classinfo)
    except TypeError:  # pragma: no cover
        # No coverage since we never call this without a class,
        # type, or tuple of classes, types, or such typles.
        return False


class Column(ComparisonMixin):
    """Represents a single attribute in DynamoDB.

    :param typedef: The type of this attribute.  Can be either a :class:`~bloop.types.Type` or
        an instance thereof.  If a type class is provided, the column will call the constructor without arguments
        to create an instance.  For example, ``Column(Integer)`` and ``Column(Integer())`` are equivalent.
    :param bool hash_key: *(Optional)* True if this is the model's hash key.
        A model must have exactly one Column with ``hash_key=True``.  Default is False.
    :param bool range_key:  *(Optional)* True if this is the model's range key.
        A model can have at most one Column with
        ``range_key=True``.  Default is False.
    :param str name: *(Optional)* The index's name in in DynamoDB. Defaults to the index’s name in the model.
    """
    def __init__(self, typedef, hash_key=False, range_key=False, name=None, **kwargs):
        self.hash_key = hash_key
        self.range_key = range_key
        self._model_name = None
        self._dynamo_name = name
        if subclassof(typedef, Type):
            typedef = typedef()
        if instanceof(typedef, Type):
            self.typedef = typedef
        else:
            raise TypeError(f"Expected {typedef} to be instance or subclass of Type")

    __hash__ = object.__hash__

    def __set_name__(self, owner, name):
        self.model = owner
        self._model_name = name

    def __set__(self, obj, value):
        self.set(obj, value)

    def __get__(self, obj, type=None):
        if obj is None:
            return self
        return self.get(obj)

    def __delete__(self, obj):
        self.delete(obj)

    def __repr__(self):
        if self.hash_key:
            extra = "=hash"
        elif self.range_key:
            extra = "=range"
        else:
            extra = ""

        # <Column[Pin.url]>
        # <Column[User.id=hash]>
        # <Column[File.fragment=range]>
        return f"<Column[{self.model.__name__}.{self.model_name}{extra}]>"

    @property
    def model_name(self):
        """Name of the model's attr that references self"""
        return self._model_name

    @property
    def dynamo_name(self):
        if self._dynamo_name is None:
            return self.model_name
        return self._dynamo_name

    def set(self, obj, value):
        if self._model_name is None:
            raise AttributeError("Can't set field without binding to model")
        obj.__dict__[self._model_name] = value
        # Notify the tracking engine that this value was intentionally mutated
        object_modified.send(self, obj=obj, column=self, value=value)

    def get(self, obj):
        if self._model_name is None:
            raise AttributeError("Can't get field without binding to model")
        try:
            return obj.__dict__[self._model_name]
        except KeyError:
            raise AttributeError(f"'{obj.__class__}' has no attribute '{self._model_name}'")

    def delete(self, obj):
        try:
            if self._model_name is None:
                raise AttributeError("Can't delete field without binding to model")
            try:
                del obj.__dict__[self._model_name]
            except KeyError:
                raise AttributeError(f"'{obj.__class__}' has no attribute '{self._model_name}'")
        finally:
            # Unlike set, we always want to mark on delete.  If we didn't, and the column wasn't loaded
            # (say from a query) then the intention "ensure this doesn't have a value" wouldn't be captured.
            object_modified.send(self, obj=obj, column=self, value=None)
