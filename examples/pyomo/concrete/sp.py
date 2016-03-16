# sp.py
from pyomo.environ import *
from sp_data import *

M = ConcreteModel()
M.x = Var(within=NonNegativeReals)

def b_rule(B, i):
  B.y = Var()
  B.l = Constraint(expr=B.y >= (c-b)*M.x + b*d[i])
  B.u = Constraint(expr=B.y >= (c+h)*M.x + h*d[i])
  return B
M.B = Block(range(1,6), rule=b_rule)

def o_rule(M):
    return sum(M.B[i].y for i in range(1,6))/5.0
M.o = Objective(rule=o_rule)

model = M

