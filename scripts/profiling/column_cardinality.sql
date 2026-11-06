-- Distinct values of one hierarchy level column. Feeds the "Cardinality"
-- column on the AttributeHierarchies sheet. Dimensions only.
-- Parameters: $relation, $column
select count(distinct $column) as distinct_values
from $relation
