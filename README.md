# League_Data_App
This application will leverage the plethora of Riot Games APIs in order to make a fully functioning real-time data and statistics application.

Functionality
- Will keep track over and display the user's match history in real time
- Will keep track of the ranked ladder, providing real time updates on champion winrates and the top 500 players
- Will display to the user important counter picks and build suggestions
- Will use the users recent match history to suggest picks that are useful against certain champions in the game (even if they are not directly laning against them.)
- Will provide the user with tips on how to lane and what their initial win conditions are given their champion and match up
- Will provide an unstructured tier list of champions for each role, that will depend solely on winrates vs. threshholds over human data interpretation
- Application will not have or ever use advertisements as means of monitization
- Application will potentially levarage agentic AI for the purposes of assisting players with a thorough analysis of their match history and suggestions for improvement


Limitations
- App will be primarily coded in Python, leading to some signification memory overhead.
- App will be developed with as few as 20 Queries a second and 100 Queries every minute. Which means the application will likely not be able to query all the data on start up.



Data Management

Due to how the data needs to be queueried from the Riot API, PUUIDs will need to be obtained and then stored, these may find themselves in this repository but they will have no real functionality with any other development key. But they might give any viewers of the repository intel how they might need to query the database and how PUUIDs look. 

Tables:
- A Primary Key Table that contains every League of Legends Champion
- Secondary Table for each Champion that shows their match ups and builds
- A plain text table that contains all of the PUUIDs for future query purposes
- A Table that contains the top 500 Players, Their Winrates, Champion Preferences, And Role.


