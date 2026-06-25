import csv
import requests
import json
import pandas as pd
import time
import ast


SummonerID_List = []
PUUID_List = []

with open("api_key.txt", "r") as f:
    api_key = f.readline()

def query_challenger():
    response = requests.get("https://na1.api.riotgames.com/lol/league/v4/challengerleagues/by-queue/RANKED_SOLO_5x5?api_key=" + api_key)
    with open("challengerleagues.json", "w") as f:
        json.dump(response.json(), f, indent=4)

def query_grandmaster():
    response = requests.get("https://na1.api.riotgames.com/lol/league/v4/grandmasterleagues/by-queue/RANKED_SOLO_5x5?api_key=" + api_key)
    with open("grandmasterleagues.json", "w") as f:
        json.dump(response.json(), f, indent=4)

def query_masters():
    response = requests.get("https://na1.api.riotgames.com/lol/league/v4/masterleagues/by-queue/RANKED_SOLO_5x5?api_key=" + api_key)
    with open("masterleagues.json", "w") as f:
        json.dump(response.json(), f, indent=4)


"""This function will query the Riot API for the match IDs played by each PUUID in the PUUID.txt list."""
def query_summoners():
    try:
        with open("puuids.txt", "r") as f:
            puuids = f.readlines()
            counter = 0
            for puuid in puuids:
                response = requests.get(f"https://americas.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids?type=ranked&start=0&count=100&api_key={api_key}")
                #Response should be of type list with elements of type string
                response_list = response.json()
                for entries in response_list:
                    print(entries)
                #Use this to get an example of the return for refined data extraction
                try:
                    with open("sampleMatchV5query.txt", "w") as f:
                        f.write(response.text)
                except Exception as e:
                    print(f"{e}, error writing to sampleMatchV5query.txt")
                """This loop will find and store all match IDs related to the top 10000 players, there WILL be duplicates, which can 
                be accounted for by another function which would require less work than finding and removing duplicates in place"""
                try:
                    with open("matchID.txt", "a") as f:
                        for entries in response_list:
                            print(f"writing {entries} to matchID.txt")
                            f.writelines(f"{entries}\n")
                except Exception as e:
                    print(f"{e}, error writing to matchID.txt")
                counter += 1
                print(f"query # {counter}")
                time.sleep(0.05)
                if counter % 100 == 0:
                    print("Query on Cooldown")
                    time.sleep(120)
    except Exception as e:
        print(f"{e}, error on querying puuids.txt")

#query_challenger()
#query_grandmaster()
#query_masters()

"""This function will extract puuids from any of the respective league jsons, in order to make a database of purely puuids which can be used to query for match history data in order to ultimately acquire champion data"""
def extract_puuids(passed : str):
    if passed == "challenger":
        try:
            with open("challengerleagues.json", "r") as f:
                challenger = json.load(f)
                challenger_puuids = []
                for entries in challenger["entries"]:
                    challenger_puuids.append(entries["puuid"])
                for entries in challenger_puuids:
                    entries = f"{entries}\n"
        except Exception as e:
            print(f"{e}, try 1")
        try:
            with open("puuids.txt", "w+") as f:
                as_is = f.readlines()
                if as_is:
                    as_is_set = set(as_is)
                    puuids_set = set(challenger_puuids)
                    identical_puuids_removed = puuids_set.difference(as_is_set)
                    f.writelines(identical_puuids_removed)
                if not as_is:
                    for entries in challenger_puuids:
                        f.write(f"{entries}\n")
        except Exception as e:
            print(f"{e}, try 2")
        return
    if passed == "grandmaster":
        try:
            with open("grandmasterleagues.json", "r") as f:
                challenger = json.load(f)
                challenger_puuids = []
                for entries in challenger["entries"]:
                    challenger_puuids.append(entries["puuid"])
                for entries in challenger_puuids:
                    entries = f"{entries}\n"
        except Exception as e:
            print(f"{e}, try 1")
        try:
            with open("puuids.txt", "w+") as f:
                as_is = f.readlines()
                if as_is:
                    as_is_set = set(as_is)
                    puuids_set = set(challenger_puuids)
                    identical_puuids_removed = puuids_set.difference(as_is_set)
                    f.writelines(identical_puuids_removed)
                if not as_is:
                    for entries in challenger_puuids:
                        f.write(f"{entries}\n")
        except Exception as e:
            print(f"{e}, try 2")
        return
    if passed == "masters":
        try:
            with open("masterleagues.json", "r") as f:
                challenger = json.load(f)
                challenger_puuids = []
                for entries in challenger["entries"]:
                    challenger_puuids.append(entries["puuid"])
                for entries in challenger_puuids:
                    entries = f"{entries}\n"
        except Exception as e:
            print(f"{e}, try 1")
        try:
            with open("puuids.txt", "w+") as f:
                as_is = f.readlines()
                if as_is:
                    as_is_set = set(as_is)
                    puuids_set = set(challenger_puuids)
                    identical_puuids_removed = puuids_set.difference(as_is_set)
                    f.writelines(identical_puuids_removed)
                if not as_is:
                    for entries in challenger_puuids:
                        f.write(f"{entries}\n")
        except Exception as e:
            print(f"{e}, try 2")
        return

#extract_puuids("challenger")
#extract_puuids("grandmaster")
#extract_puuids("masters")
query_summoners()

#Our Outline

"""Fetch Seed Players by Rank using the League-V4 endpoint, this will return a massive JSON
    array of high-elo players containing their summonerID"""

"""Convert Summoner ID to PUUID by looping through theses top players using the SUMMONER-V4 End point
to quickly translate their summoner ID to PUUID then store these in a local SQL environment"""

"""Query the MATCH-V5" endpoint for each target player. These yields a list of unique match strings"""

"""When you call the specific match timeline or details endpoint, the response JSON contains the data for all 10 participants in that match
This includes their champion played, win/loss outcome, and their respective PUUIDs"""

"""From here formulate a dataframe using the data extracted from each game and slot in the data into dataframes the aggregate win/losses, builds, cspm data, etc."""

#Important Info

"""PUUIDs are universal and unique identifiers for my development environment, this means that for each new PUUID I find, I should store it in a file
These PUUIDs are unique to my developer keys and thus would not be usable for any other developer."""

"""We will need to use libraries that slow down the program to stop my program from exceeding queries per second limit
"""



