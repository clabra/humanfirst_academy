"""
python get_plan_and_usage.py -u <username> -p <password>

Retrieves the subscription plan and usage for the organisation a user belongs to

Set HF_USERNAME and HF_PASSWORD as environment variables
"""
# *********************************************************************************************************************

# standard imports
import json

# third party imports
import click
import pandas
import humanfirst


@click.command()
@click.option('-u', '--username', type=str, default='',
              help='HumanFirst username if not setting HF_USERNAME environment variable')
@click.option('-p', '--password', type=str, default='',
              help='HumanFirst password if not setting HF_PASSWORD environment variable')
def main(username: str, password: int):
    '''Main'''

    # authorise
    hf_api = humanfirst.apis.HFAPI(username=username, password=password)

    # get plan info as a json
    print("Plan info")
    plan_dict = hf_api.get_plan()
    print(json.dumps(plan_dict, indent=2))
    print("\n")

    # get usage info
    usage_dict = hf_api.get_usage()

    # turn into dfs for df.to_csv or similar
    print("dataPoints.conversationSets")
    print(pandas.json_normalize(usage_dict["dataPoints"]["conversationSets"]))
    print("\n")
    print("dataPoints.workspaces")
    print(pandas.json_normalize(usage_dict["dataPoints"]["workspaces"]))
    print("\n")
    df = pandas.json_normalize(usage_dict)
    print("Usage summary")
    df.drop(columns=["dataPoints.conversationSets","dataPoints.workspaces"],inplace=True)
    print(df)
    print("\n")


if __name__ == '__main__':
    main()  # pylint: disable=no-value-for-parameter
