import requests
import json
import pandas as pd

SummonerID_List = []
PUUID_List = []

with open("api_key.txt", "r") as f:
    api_key = f.readline()

def query_challenger():
    response = requests.get("https://riotgames.com/lol/league/v4/challengerleagues/by-queue/RANKED_SOLO_5x5?api_key=" + api_key)
    with open("challengerleagues.json", "w") as f:
        f.write(response.text)

def query_grandmaster():
    response = requests.get("https://riotgames.com/lol/league/v4/grandmasterleagues/by-queue/RANKED_SOLO_5x5?api_key=" + api_key)
    with open("grandmasterleagues.json", "w") as f:
        f.write(response.text)

def query_masters():
    response = requests.get("https://riotgames.com/lol/league/v4/masterleagues/by-queue/RANKED_SOLO_5x5?api_key=" + api_key)
    with open("masterleagues.json", "w") as f:
        f.write(response.text)

        

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



