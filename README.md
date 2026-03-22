# trade_robot
a robot that helps decide when to buy and sell

## Install the environment
It is recommended that you use a virtual environment to install the dependencies.
1. Create a python virtual environment: ```virtualenv -p python3 .venv```
2. Activate the environment: ```source .venv/bin/activate```
3. Install the dependencies: ```pip install -r requirements.txt```
If you want to update the requirements.txt, just run ```pip freeze > requirements.txt```.
4. Deactivate the environment: ```deactivate```


## Some Git commands:
```
ssh-keygen
cat ~/.ssh/id_rsa.pub
git checkout .  # 无视并放弃所有更新，方便下一步进行pull（或者运行git reset --hard也可以）
git remote set-url origin git@github.com:yuanyuan2000/trade_robot.git  # 将origin地址设置为仓库的Git地址（当使用ssh进行通信时，如果使用http则要用http地址）
git pull origin
```