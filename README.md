# Part-Manager_Remote_worker
This is the remote worker code for the part manager PLM system

##How to install

clone this git repo

copy the env.example file to .env and edit the values for your installation
''json
PM_SERVER_URL=https://your-parts-manager-server
PM_WORKER_NAME=work1
PM_POLL_SECONDS=15
FREECAD_CMD=freecadcmd
''

sudo docker compose build

sudo docker compose up -d

It should connect to your server. You will have to into the server admin panel and approve the worker

<img width="1808" height="476" alt="image" src="https://github.com/user-attachments/assets/7223511b-addb-4c70-bdf9-ce0d1d12927b" />


You will also need to upload some control scripts to the server and enabled by the admin user
<img width="1636" height="255" alt="image" src="https://github.com/user-attachments/assets/3938b465-b53e-471a-903b-eae5d407e050" />

you will need to assign a 'job type' to the script when you upload it to the server


CAD_TO_GLB  - This will convert the freecad data to a light weight format that can be viewed in the part detail page

The scripts can be found here in the scripts folder
