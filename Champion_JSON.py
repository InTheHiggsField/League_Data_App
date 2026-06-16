import json
import requests

def get_live_champion_list():
    #Get Patch Version
    version_url = "https://ddragon.leagueoflegends.com/api/versions.json"
    latest_version = requests.get(version_url).json()[0]

    data_url = f"https://ddragon.leagueoflegends.com/cdn/{latest_version}/data/en_US/champion.json"
    champion_data = requests.get(data_url).json()["data"]

    champion_names = [info["name"] for info in champion_data.values()]

    return sorted(champion_names)

def develop_table():
    champion_dict = {}
    champions_list = get_live_champion_list()
    role_list = ["top" , "jungle", "mid", "bot", "support"]

    for champion in champions_list:
        for role in role_list:
            champion_dict[f"{champion}_{role}"] = {"Winrate" : 0, "Ban Rate" : 0, "Matches": 0, "Countered_By" :[], "Counters" : [], "Role" : f"{role}"}
    with open("Champion.json", "w") as f:
        json.dump(champion_dict, f, indent = 4)


develop_table()


