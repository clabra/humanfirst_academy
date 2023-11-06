#!/usr/bin/env python # pylint: disable=missing-module-docstring
# -*- coding: utf-8 -*-
# ***************************************************************************80*************************************120
#
# python ./summarize/summarize_transcripts_generic.py                                    # pylint: disable=invalid-name
#
# text mode received limited testing
#
# *********************************************************************************************************************

# standard imports
import json
import os
import logging
from multiprocessing import Pool
from time import perf_counter
import datetime
import sys
from pathlib import Path
import re

# 3rd party imports
import openai
import pandas
import numpy
import click
import tiktoken

# custom imports
dir_path = os.path.dirname(os.path.realpath(__file__))
hf_module_path = str(Path(dir_path).parent)
sys.path.insert(1, hf_module_path)

import humanfirst_nlg # pylint: disable=wrong-import-position


class OpenAITooManyTokens(Exception):
    """Thrown if input data too large before calling the relevant model"""

    def __init__(self, tokens: int, limit: int, convo_id: str):
        self.message = f'Data tokens {tokens} exceeds OpenAI model limit {limit} - convo id is {convo_id}'
        super().__init__(self.message)


@click.command()
@click.option('-i', '--input_filepath', type=str, required=True,
              help='Path containing HF Unlabelled conversations in json format or a txt format if utterances')
@click.option('-a', '--openai_api_key', type=str, required=True, help='OpenAI API key')
@click.option('-p', '--prompt', type=str, default='./prompts/abcd_example_prompt.txt',
              help='location of prompt file to read')
@click.option('-t', '--output_tokens', type=int, default=500, help='Tokens to reserve for output')
@click.option('-n', '--num_cores', type=int, default=2, help='Number of cores for parallelisation')
@click.option('-s', '--sample_size', type=int, default=0, help='Number of conversations/utterances to sample')
@click.option('-m', '--model_override', default='', type=str, required=False,
              help='Use this model name')
@click.option('-l', '--log_file_path', type=str, default='./logs', help='Server log file path')
@click.option('-b', '--drop_list', type=str, default='', help='Comma separated list of conversations to drop')
@click.option('-o', '--output_file_path', type=str, default='./summaries', help='Summaries output file path')
@click.option('-r', '--rewrite', is_flag=True, type=bool, default=False,
              help='If present will rewrite (overwrite) all previous summaries')
@click.option('-d', '--dummy', is_flag=True, type=bool, default=False, help='Skip the actual openai call')
@click.option('-v', '--verbose', is_flag=True, type=bool, default=False,
              help='Set logging level to DEBUG otherwise INFO')
@click.option('-o', '--omitrole', is_flag=True, type=bool, required=False, default=False,
              help='Omit role where conversation is only one utterance by the client')
def main(input_filepath: str,
         openai_api_key: str,
         num_cores: int,
         prompt: str,
         output_tokens: int,
         sample_size: str,
         model_override: str,
         log_file_path: str,
         drop_list: str,
         output_file_path: str,
         rewrite: bool,
         dummy: bool,
         verbose: bool,
         omitrole: bool) -> None:
    '''Main Function'''
    process(input_filepath, openai_api_key, num_cores, prompt, output_tokens,
            sample_size, model_override, log_file_path, drop_list, output_file_path, 
            rewrite, dummy, verbose, omitrole)


def process(input_filepath: str,
            openai_api_key: str,
            num_cores: int,
            prompt: str,
            output_tokens: int,
            sample_size: str,
            model_override: str,
            log_file_path: str,
            drop_list: str,
            output_file_path: str,
            rewrite: bool,
            dummy: bool,
            verbose: bool,
            omitrole: bool):
    '''Summarization of Conversations'''

    # set log level
    log_level = logging.INFO
    if verbose:
        log_level = logging.DEBUG

    # determine curtent directory
    dir_path = os.path.dirname(os.path.realpath(__file__))
    if not dir_path.endswith("/"):
        dir_path = f'{dir_path}/'

    # logging config
    if log_file_path.startswith("./"):
        now = datetime.datetime.now().isoformat()
        log_file_path = f'{dir_path}{log_file_path}/summarize_transcripts_generic_{now}.log'
    logging.basicConfig(
        filename=log_file_path,
        filemode='w',
        level=log_level,
        format='%(asctime)s - %(name)s - %(process)d - %(levelname)s -- %(message)s', datefmt='%d-%b-%y %H:%M:%S')
    logging.info("Logging to: %s", log_file_path)

    # get prompt
    if prompt.startswith("./"):
        prompt = prompt[2:]
    prompt = open(f'{dir_path}{prompt}', mode="r", encoding="utf8").read()
    logging.info("Prompt is: \n %s", prompt)

    openai.api_key = openai_api_key

    # load input data
    if input_filepath.endswith(".json"):
        with open(input_filepath, mode="r", encoding="utf8") as file:
            data = json.load(file)
        df = pandas.json_normalize(data=data["examples"], sep="-",)

        # enforce id is string
        df["context-context_id"] = df["context-context_id"].astype(str)

        # give a sequence number to each utterance
        df = df.sort_values(["context-context_id", "created_at"])
        df['seq'] = df.groupby("context-context_id").cumcount()

        mode = "conversation"

        # set context-context_id and seq as index
        df.set_index(["context-context_id", "seq"], drop=True, inplace=True)

    elif input_filepath.endswith(".txt"):
        with open(input_filepath, mode="r", encoding="utf8") as file:
            data = file.read()
            data = data.split("\n")
        df = pandas.DataFrame(data=data, columns=["text"],)

        df["context-context_id"] = df["text"]
        df["seq"] = 1
        mode = "text"
        df.set_index(["context-context_id", "seq"], drop=False, inplace=True)
    else:
        raise Exception("Unrecognised type")

    # work out what's been run before
    if output_file_path.startswith('./'):
        output_file_path = output_file_path[2:]
        output_file_path = f'{dir_path}{output_file_path}'
    if not output_file_path.endswith('/'):
        output_file_path = f'{output_file_path}/'
    completed_df = get_completed_files(output_file_path)
    df = df.join(completed_df)
    df["completed"] = df["completed"].fillna(False)

    # default to run everything
    df["skip"] = False

    # skip if rewrite false
    if not rewrite:
        df["skip"] = df["completed"]

    # get all the context-context_ids
    context_ids = set(df.index.unique(level=0))
    completed_context_ids = set(completed_df.index.unique(level=0))

    # skip don't resample completed
    if not rewrite:
        context_ids = context_ids - completed_context_ids
    context_ids = list(context_ids)

    # implement dummy run flag
    if dummy:
        df["skip"] = dummy

    # take the next sample size to process
    if sample_size > 0:
        context_ids = context_ids[0:sample_size]
    logging.info(
        "In this run are going to process this many context_ids: %s", len(context_ids))
    logging.info("First up to 10: %s", context_ids[0:10])

    # select down the data frame to that.
    df = df.loc[context_ids, :]

    if mode == "conversation":
        # ABCD example has a difference between metdata-abcd_role and context-role
        # here we are going to use a generic client|expert HF standard but that is perhaps not optimum
        # print(df.loc["1000",["text","metadata-abcd_role","context-role"]])
        # build the conversation line for prompt
        df["prompt_line"] = df["context-role"] + ": " + df["text"]
    elif mode == "text":
        df["prompt_line"] = df["text"]
    else:
        raise Exception(f"Not recognised mode: {mode}")

    # reduce our data frame just to the data we need
    df = df[["context-context_id","prompt_line", "skip", "completed"]]
    assert isinstance(df, pandas.DataFrame)

    if mode == "conversation":
        # join all the prompt_lines together into the conversation text by
        # the context-context_id, skip and whether completed
        df = df.groupby(["context-context_id", "skip", "completed"]
                        )['prompt_line'].apply('\n'.join).reset_index()
        df.set_index("context-context_id", inplace=True, drop=False)
        df.rename(columns={"prompt_line": "conversation"}, inplace=True)

        # assemble the final prompt with the {{ conversation }} replaced
        re_conversation = humanfirst_nlg.get_nlg_tag_regex("conversation")

        # drops off any conversations from processing
        if drop_list != "":
            drop_list = drop_list.split(",")
            df.drop(labels=drop_list,inplace=True)

        # assemble the final prompt with the {{ conversation }} replaced
        df['prompt'] = df['conversation'].apply(
            merge_prompt_and_string, args=[prompt, re_conversation])
    elif mode == "text":
        # assemble the final prompt with the {{ conversation }} replaced
        re_text = humanfirst_nlg.get_nlg_tag_regex("text")

        # drops off any conversations from processing
        if drop_list != "":
            drop_list = drop_list.split(",")
            df.drop(labels=drop_list,inplace=True)

        df["prompt"] = df["prompt_line"].apply(
            merge_prompt_and_string, args=[prompt, re_text])
        df.set_index("context-context_id", inplace=True, drop=False)
    else:
        raise Exception(f"Not recognised mode: {mode}")

    # estimate the tokens - use the 4k one, and then bump up if needed with factor of safety
    df['tokens'] = df["prompt"].apply(count_tokens,
                                      args=[tiktoken.encoding_for_model("gpt-3.5-turbo")])
    mean_input = df["tokens"].mean()
    logging.info('Mean input tokens is: %s', mean_input)
    logging.info('Output tokens is:  %s', output_tokens)
    in_and_out_tokens = mean_input + output_tokens
    per_second = 1500 / in_and_out_tokens
    logging.info('Per second rate is max %2f', per_second)

    # work out model for each row
    if model_override != '':
        df['model'] = model_override
    else:
        # Below line send the tokens as a series.
        # This helps in providing the index while raising OpenAITooManyTokens exception
        df['model'] = df[['tokens']].apply(calculate_which_model,args=[output_tokens],axis=1)

    # max_tokens
    df['max_tokens'] = output_tokens

    print(df[["context-context_id","prompt"]])

    # add the output location for the file
    df['summary_path'] = output_file_path + df['context-context_id'] + ".txt"

    # verbose setting
    df["verbose"] = verbose

    print(df)

    # parallelization
    pool = Pool(num_cores)
    dfs = numpy.array_split(df, num_cores)
    pool_results = pool.map(parallelise_calls, dfs)
    pool.close()
    pool.join()
    df = pandas.concat(pool_results, axis=1)


def get_completed_files(output_file_path: str) -> pandas.DataFrame:
    '''Create a dataframe of ids that have already been created'''
    file_names = os.listdir(output_file_path)
    completed_ids = []
    for file_name in file_names:
        if file_name.endswith(".txt"):
            completed_ids.append(file_name[0:-4])
    completed_df = pandas.DataFrame(
        completed_ids, columns=['context-context_id'])
    completed_df['completed'] = True
    completed_df.set_index(['context-context_id'], inplace=True, drop=True)
    return completed_df


def merge_prompt_and_string(string: str, prompt: str, re_tag: re) -> str:
    ''' Replaces tags with the actual string'''

    return re_tag.sub(string,prompt)


def parallelise_calls(df: pandas.DataFrame) -> pandas.DataFrame:
    '''Parallelise dataframe processing'''
    return df.apply(call_api, axis=1)


def call_api(row: pandas.Series) -> pandas.Series:
    '''Call OpenAI API for summarization'''

    start_time = perf_counter()
    logging.info(
        "Calling OpenAI model %s to summarize conversation: %s", row["model"], row.name)
    if row["verbose"]:
        logging.info("Prompt: %s", row["prompt"])

    row["summary"] = ''
    row["total_tokens"] = 0
    if not row["skip"]:
        try:
            row = summarize(row, output_tokens=row["max_tokens"])
        except Exception as e:  # pylint: disable=broad-except
            logging.error('Exception for %s is %s', row.name, e)
            return row["summary"]

    logging.info('Total tokens for conversation id %s is %s',
                 row.name, row["total_tokens"])
    logging.info('Conversation - %s is summarized', row.name)

    # write the file to output if not in dummy mode
    if not row["skip"]:
        with open(row["summary_path"], mode="w", encoding="utf8") as file:
            file.write(row["summary"])

    logging.info('Summary is saved at: %s', row["summary_path"])
    end_time = perf_counter()
    logging.info('Took %.2f seconds', end_time-start_time)
    return row


def calculate_which_model(row: pandas.Series, output_tokens: int) -> str:
    '''Works out which model for data line based on tokens'''

    tokens=row["tokens"]
    # dec to bin conversion gives margin of error
    if tokens + output_tokens < 4000:
        return "gpt-3.5-turbo"
    elif tokens + output_tokens < 16000:
        return "gpt-3.5-turbo-16k"
    elif tokens + output_tokens < 32000:
        return "gpt-4-32k"
    else:
        raise OpenAITooManyTokens(tokens + output_tokens, 32000, row.name)


def summarize(row: pandas.Series, output_tokens: int) -> str:
    '''Summarizes single conversation using prompt'''

    response = openai.ChatCompletion.create(
        model=row["model"],
        messages=[
            {"role": "user", "content": row["prompt"]}
        ],
        temperature=0.0,
        max_tokens=output_tokens,
        top_p=1,
        frequency_penalty=0.0,
        presence_penalty=0.0
    )
    row["summary"] = response.choices[0].message.content + "\n"
    row["total_tokens"] = response.usage.total_tokens
    return row


def count_tokens(text: str, encoding):
    """Returns the number of tokens in a text string."""
    return len(encoding.encode(text))


if __name__ == "__main__":
    main()  # pylint: disable=no-value-for-parameter
