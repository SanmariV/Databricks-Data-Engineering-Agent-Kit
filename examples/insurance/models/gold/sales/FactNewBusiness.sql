with

new_business as (

    select * from {{ ref('sales.NewBusinessPolicy') }}

),

agent as (

    select * from {{ ref('distribution.DimAgent') }}

)

select
    new_business.Policy_BK,
    coalesce(agent.DimAgent_SK, -1) as DimAgent_SK,
    new_business.ProductCode,
    new_business.AnnualPremiumAmount,
    new_business.IssueDate
from new_business
left join agent
    on agent.Agent_BK = new_business.Agent_BK
    and new_business.IssueDate between agent.StartDate and agent.EndDate
