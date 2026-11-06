-- New business = an issued policy (rule agreed with the business owner).
select
    policy.Policy_BK,
    policy.Agent_BK,
    policy.ProductCode,
    policy.AnnualPremiumAmount,
    policy.IssueDate
from {{ ref('policy_admin.Policy') }} as policy
where policy.IssueDate is not null
