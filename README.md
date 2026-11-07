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

