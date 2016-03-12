from tensorframes.shape_infer import *

import tensorflow as tf

from pyspark import RDD, SparkContext
from pyspark.sql import SQLContext, Row, DataFrame
import logging

__all__ = ['reduce_rows', 'map_rows', 'reduce_blocks', 'map_blocks', 'analyze', 'print_schema']

_sc = None
_sql = None
logger = logging.getLogger('tensorframes')

def _java_api():
    """
    Loads the PythonInterface object (lazily, because the spark context needs to be initialized first).
    """
    global _sc, _sql
    javaClassName = "org.tensorframes.impl.DebugRowOps"
    if _sc is None:
        _sc = SparkContext._active_spark_context
        logger.info("Spark context = " + str(_sc))
        _sql = SQLContext(_sc)
    _jvm = _sc._jvm
    # You cannot simply call the creation of the the class on the _jvm due to classloader issues
    # with Py4J.
    return _jvm.Thread.currentThread().getContextClassLoader().loadClass(javaClassName) \
        .newInstance()

def _get_shape(node):
    l = node.get_shape().as_list()
    return [-1 if x is None else x for x in l]

def _add_graph(graph, builder):
    gser = graph.as_graph_def().SerializeToString()
    gbytes = bytearray(gser)
    builder.graph(gbytes)

def _add_shapes(graph, builder, fetches):
    names = [fetch.name for fetch in fetches]
    shapes = [_get_shape(fetch) for fetch in fetches]
    # We still need to do the placeholders, it seems their shape is not passed in when some
    # dimensions are unknown
    ph_names = []
    ph_shapes = []
    for n in graph.as_graph_def().node:
        # Just the input nodes:
        if not n.input:
            op_name = n.name
            # Simply get the default output for now, assume that the nodes have only one output
            t = graph.get_tensor_by_name(op_name + ":0")
            ph_names.append(t.name)
            ph_shapes.append(_get_shape(t))
    logger.info("fetches: %s %s", str(names), str(shapes))
    logger.info("inputs: %s %s", str(ph_names), str(ph_shapes))
    builder.shape(names + ph_names, shapes + ph_shapes)

def _check_fetches(fetches):
    is_list_fetch = isinstance(fetches, (list, tuple))
    if not is_list_fetch:
        return [fetches]
    return fetches

def _get_graph(fetches):
    graph = tf.get_default_graph()
    fetch_names = [_validate_fetch(graph, fetch) for fetch in fetches]
    logger.info("Fetch names: %s", str(fetch_names))
    # String the output index
    col_names = [s.split(":")[0] for s in fetch_names]
    if len(set(col_names)) != len(col_names):
        raise ValueError("Could not infer a list of unique names for the columns: %s" % str(fetch_names))
    return graph

def _unpack_row(jdf):
    df = DataFrame(jdf, _sql)
    row = df.first()
    l = list(row)
    if len(l) == 1:
        return l[0]
    return l


def reduce_rows(fetches, dframe):
    """ Applies the fetches on pairs of rows, so that only one row of data remains in the end. The order in which
    the operations are performed on the rows is unspecified.

    The `fetches` argument may be a list of graph elements or a single
    graph element. A graph element can be of the following type:

    * If the *i*th element of `fetches` is a
      `Tensor`, the *i*th return value will be a numpy ndarray containing the value of that tensor.

    There is no support for sparse tensor objects yet.

    This transform not lazy and is performed when called.

    In order to perform the reduce operation, the fetches must follow some naming conventions: for each fetch called
    for example 'z', there must be two placeholders 'z_1' and 'z_2' that will be fed with the input data. The shapes
    and the dtypes of z, z_1 and z_2 must be the same.

    Args:
      fetches: A single graph element, or a list of graph elements
        (described above).
      dframe: A DataFrame object. The columns of the tensor frame will be fed into the fetches at execution.

    Returns: a list of numpy arrays, one for each of the fetches, or a single numpy array if there is but one fetch.

    :param fetches: see description above
    :param dframe: a Spark DataFrame
    :return: a list of numpy arrays
    """
    fetches = _check_fetches(fetches)
    graph = _get_graph(fetches)
    builder = _java_api().reduce_rows(dframe._jdf)
    _add_graph(graph, builder)
    _add_shapes(graph, builder, fetches)
    df = builder.buildRow()
    return _unpack_row(df)

def map_rows(fetches, dframe):
    """ Transforms a DataFrame into another DataFrame row by row, by adding new fields for each fetch.

    The `fetches` argument may be a list of graph elements or a single
    graph element. A graph element can be one of the following type:

    * a TensorFlow's Tensor object. The shape and the dtype of the tensor will dictate the structure of the column

    Note on computations: unlike the TensorFlow execution engine, the result is lazy and will not be computed until
    requested. However, all the fetches and the computation graph are frozen when this function is called.

    The inputs of the fetches must all be constants or placeholders. The placeholders must have the name of existing
    fields in the dataframe, and they must have the same dtype as the placeholder (no implicit casting is performed on
    the input). Additionally, they must have a tensor shape that is compatible with the shape of the elements in that
    field. For example, if the field contains scalar, only scalar shapes are accepted, etc.

    The names of the fetches must be all different from the names of existing columns, otherwise an error is returned.

    This method works row by row. If you want a more efficient method that can work on batches of rows, consider using
    [map_blocks] instead.

    Args:
      fetches: A single graph element, or a list of graph elements
        (described above).
      dframe: A Spark DataFrame object. The columns of the tensor frame will be fed into the fetches at execution.

    Returns: a DataFrame. The columns and their names are inferred from the names of the fetches.

    :param fetches: see description above
    :param dframe: a Spark DataFrame
    :return: a Spark DataFrame
    """
    fetches = _check_fetches(fetches)
    graph = _get_graph(fetches)
    builder = _java_api().map_rows(dframe._jdf)
    _add_graph(graph, builder)
    _add_shapes(graph, builder, fetches)
    jdf = builder.buildDF()
    return DataFrame(jdf, _sql)

def map_blocks(fetches, dframe):
    """ Transforms a DataFrame into another DataFrame block by block, by adding new fields for each fetch.

    The `fetches` argument may be a list of graph elements or a single
    graph element. A graph element can be one of the following type:

    * a TensorFlow's Tensor object. The shape and the dtype of the tensor will dictate the structure of the column

    Note on computations: unlike the TensorFlow execution engine, the result is lazy and will not be computed until
    requested. However, all the fetches and the computation graph are frozen when this function is called.

    The inputs of the fetches must all be constants or placeholders. The placeholders must have the name of existing
    fields in the dataframe, and they must have the same dtype as the placeholder (no implicit casting is performed on
    the input). Additionally, they must have a tensor shape that is compatible with the shape of the elements in that
    field. For example, if the field contains scalar, only scalar shapes are accepted, etc.

    The names of the fetches must be all different from the names of existing columns, otherwise an error is returned.

    This method does not work when rows contains vectors of different sizes. In this case, you must use [map_rows].

    Args:
      fetches: A single graph element, or a list of graph elements
        (described above).
      dframe: A Spark DataFrame object. The columns of the tensor frame will be fed into the fetches at execution.

    Returns: a DataFrame. The columns and their names are inferred from the names of the fetches.

    :param fetches: see description above
    :param dframe: a Spark DataFrame
    :return: a Spark DataFrame
    """
    fetches = _check_fetches(fetches)
    # We are not dealing for now with registered expansions, but this is something we should add later.
    graph = _get_graph(fetches)
    builder = _java_api().map_blocks(dframe._jdf)
    _add_graph(graph, builder)
    _add_shapes(graph, builder, fetches)
    jdf = builder.buildDF()
    return DataFrame(jdf, _sql)

def reduce_blocks(fetches, dframe):
    """ Applies the fetches on blocks of rows, so that only one row of data remains in the end. The order in which
    the operations are performed on the rows is unspecified.

    The `fetches` argument may be a list of graph elements or a single
    graph element. A graph element can be of the following type:

    * If the *i*th element of `fetches` is a
      `Tensor`, the *i*th return value will be a numpy ndarray containing the value of that tensor.

    There is no support for sparse tensor objects yet.

    This transform not lazy and is performed when called.

    In order to perform the reduce operation, the fetches must follow some naming conventions: for each fetch called
    for example 'z', there must be one placeholder 'z_input'. The dtype of 'z' and 'z_input' must be the same, and
    the shape of 'z_input' must be one degree higher than 'z'. For example, if 'z' is scalar, then 'z_input' must be
    a vector with unknown dimension.

    Args:
      fetches: A single graph element, or a list of graph elements
        (described above).
      dframe: A DataFrame object. The columns of the tensor frame will be fed into the fetches at execution.

    Returns: a list of numpy arrays, one for each of the fetches, or a single numpy array if there is but one fetch.

    :param fetches: see description above
    :param dframe: a Spark DataFrame
    :return: a list of numpy arrays
    """
    fetches = _check_fetches(fetches)
    graph = _get_graph(fetches)
    builder = _java_api().reduce_blocks(dframe._jdf)
    _add_graph(graph, builder)
    _add_shapes(graph, builder, fetches)
    df = builder.buildRow()
    return _unpack_row(df)

def print_schema(dframe):
    """
    Prints the schema of the dataframe, including all the metadata that describes tensor information.

    TODO: explain the data

    :param dframe: a Spark DataFrame
    :return: nothing
    """
    print(_java_api().explain(dframe._jdf))

def analyze(dframe):
    """ Analyzes a Spark DataFrame for the tensor content, and returns a new dataframe with extra metadata that
     describes the numerical shape of the content.

     This method is useful when a dataframe contains non-scalar tensors, for which the shape must be checked beforehand.

     Note: nullable fields are not accepted.

     The function [print_schema] lets users introspect the information added to the DataFrame.

    :param dframe: a Spark DataFrame
    :return: a Spark DataFrame with metadata information embedded.
    """
    return DataFrame(_java_api().analyze(dframe._jdf), _sql)

def _validate_fetch(graph, fetch):
    try:
        fetch_t = graph.as_graph_element(fetch, allow_tensor=True,
                                         allow_operation=True)
        # For now, do not make a difference between a subfetch and a target
        return fetch_t.name
    except TypeError as e:
        raise TypeError('Fetch argument %r has invalid type %r, '
                        'must be a string or Tensor. (%s)'
                        % (fetch, type(fetch), str(e)))
    except ValueError as e:
        raise ValueError('Fetch argument %r cannot be interpreted as a '
                         'Tensor. (%s)' % (fetch, str(e)))
    except KeyError as e:
        raise ValueError('Fetch argument %r cannot be interpreted as a '
                         'Tensor. (%s)' % (fetch, str(e)))