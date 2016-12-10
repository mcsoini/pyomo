import pickle

import pyutilib.th as unittest
from pyomo.core.base.component_interface import (ICategorizedObject,
                                                 IComponent)
from pyomo.core.tests.unit.test_component_dict import \
    _TestComponentDictBase
from pyomo.core.tests.unit.test_component_list import \
    _TestComponentListBase
from pyomo.core.base.component_parameter import (IParameter,
                                                 parameter,
                                                 parameter_dict,
                                                 parameter_list)
from pyomo.core.base.component_variable import variable
from pyomo.core.base.param import Param
from pyomo.core.base.component_block import block
from pyomo.core.base.set_types import (RealSet,
                                       IntegerSet)
from pyomo.core.base.numvalue import (NumericValue,
                                      is_fixed,
                                      is_constant,
                                      potentially_variable)

class Test_parameter(unittest.TestCase):

    def test_pickle(self):
        p = parameter(value=1.0)
        self.assertEqual(p.value, 1.0)
        self.assertEqual(p.parent, None)
        pup = pickle.loads(
            pickle.dumps(p))
        self.assertEqual(pup.value, 1.0)
        self.assertEqual(pup.parent, None)
        b = block()
        b.p = p
        self.assertIs(p.parent, b)
        bup = pickle.loads(
            pickle.dumps(b))
        pup = bup.p
        self.assertEqual(pup.value, 1.0)
        self.assertIs(pup.parent, bup)

    def test_init(self):
        p = parameter()
        self.assertTrue(p.parent is None)
        self.assertEqual(p.ctype, Param)
        self.assertEqual(p.value, None)
        self.assertEqual(p(), None)
        p.value = 1
        self.assertEqual(p.value, 1)
        self.assertEqual(p(), 1)

    def test_type(self):
        p = parameter()
        self.assertTrue(isinstance(p, ICategorizedObject))
        self.assertTrue(isinstance(p, IComponent))
        self.assertTrue(isinstance(p, IParameter))
        self.assertTrue(isinstance(p, NumericValue))

    def test_is_constant(self):
        p = parameter()
        self.assertEqual(p.is_constant(), False)
        self.assertEqual(is_constant(p), False)
        p.value = 1.0
        self.assertEqual(p.is_constant(), False)
        self.assertEqual(is_constant(p), False)

    def test_is_fixed(self):
        p = parameter()
        self.assertEqual(p.is_fixed(), True)
        self.assertEqual(is_fixed(p), True)
        p.value = 1.0
        self.assertEqual(p.is_fixed(), True)
        self.assertEqual(is_fixed(p), True)

    def test_potentially_variable(self):
        p = parameter()
        self.assertEqual(p._potentially_variable(), False)
        self.assertEqual(potentially_variable(p), False)
        p.value = 1.0
        self.assertEqual(p._potentially_variable(), False)
        self.assertEqual(potentially_variable(p), False)

    def test_polynomial_degree(self):
        p = parameter()
        self.assertEqual(p.polynomial_degree(), 0)
        self.assertEqual((p**2).polynomial_degree(), 0)
        self.assertEqual(p.value, None)
        with self.assertRaises(ValueError):
            (p**2)()
        p.value = 1.0
        self.assertEqual(p.polynomial_degree(), 0)
        self.assertEqual((p**2).polynomial_degree(), 0)
        self.assertEqual(p.value, 1.0)
        self.assertEqual((p**2)(), 1.0)

class Test_parameter_dict(_TestComponentDictBase,
                          unittest.TestCase):
    _container_type = parameter_dict
    _ctype_factory = lambda self: parameter()

class Test_parameter_list(_TestComponentListBase,
                           unittest.TestCase):
    _container_type = parameter_list
    _ctype_factory = lambda self: parameter()

if __name__ == "__main__":
    unittest.main()
