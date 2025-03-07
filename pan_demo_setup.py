import os
import time
import subprocess
import json
import urllib3
import requests
import cyperf


class CyPerfUtils(object):
    class color:
       PURPLE = '\033[95m'
       CYAN = '\033[96m'
       DARKCYAN = '\033[36m'
       BLUE = '\033[94m'
       GREEN = '\033[92m'
       YELLOW = '\033[93m'
       RED = '\033[91m'
       BOLD = '\033[1m'
       UNDERLINE = '\033[4m'
       END = '\033[0m'

    def __init__(self, controller, username="", password="", license_server=None, license_user="", license_password="", eula_accept_interactive=True):
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self.controller               = controller
        self.host                     = f'https://{controller}'
        self.license_server           = license_server
        self.license_user             = license_user
        self.license_password         = license_password
        self.api_ready_wait_time      = 2

        self.configuration            = cyperf.Configuration(host=self.host,
                                                             username=username,
                                                             password=password,
                                                             eula_accept_interactive=eula_accept_interactive)
        self.configuration.verify_ssl = False
        self.api_client               = cyperf.ApiClient(self.configuration)
        self.added_license_servers    = []

        #TBD: this defaults to a timeout of 10m, should we take a custom timeout?
        self.api_client.wait_for_controller_up()

        if self.license_server:
            self.update_license_server()

        self.agents = {}
        agents_api  = cyperf.AgentsApi(self.api_client)
        agents      = agents_api.get_agents()
        for agent in agents:
            self.agents[agent.ip] = agent

    def _call_api(self, func):
        while 1:
            try:
                return func()
            except cyperf.exceptions.ServiceException as e:
                time.sleep(self.api_ready_wait_time)

    def _update_license_server(self):
        if not self.license_server or self.license_server == self.controller:
            return
        license_api = cyperf.LicenseServersApi(self.api_client)
        try:
            response = license_api.get_license_servers()
            for server in response:
                if server.host_name == self.license_server:
                    if 'ESTABLISHED' == server.connection_status:
                        self.added_license_servers.append(server)
                        print(f'License server {self.license_server} is already configured')
                        return
                    license_api.delete_license_servers(str(server.id))
                    waitTime = 5 # seconds
                    print (f'Waiting for {waitTime} seconds for the license server deletion to finish.')
                    time.sleep(waitTime) # How can I avoid this sleep????
                    break
                    
            lServer = cyperf.LicenseServerMetadata(host_name=self.license_server,
                                                   trust_new=True,
                                                   user=self.license_user,
                                                   password=self.license_password)
            print (f'Configuring new license server {self.license_server}')
            newServers = license_api.create_license_servers(license_server_metadata=[lServer])
            while newServers:
                for server in newServers:
                    s = license_api.get_license_servers_by_id(str(server.id))
                    if 'IN_PROGRESS' != s.connection_status:
                        newServers.remove(server)
                        self.added_license_servers.append(server)
                        if 'ESTABLISHED' == s.connection_status:
                            print(f'Successfully added license server {s.host_name}')
                        else:
                            raise Exception(f'Could not connect to license server {s.host_name}')
                time.sleep(1)
        except cyperf.ApiException as e:
            raise (e)

    def _remove_license_server(self):
        license_api = cyperf.LicenseServersApi(self.api_client)
        for server in self.added_license_servers:
            try:
                license_api.delete_license_servers(str(server.id))
            except cyperf.ApiException as e:
                print(f'{e}')

    def update_license_server(self):
        self._call_api(self._update_license_server)

    def remove_license_server(self):
        self._call_api(self._remove_license_server)

    def load_configuration_files(self, configuration_files=[]):
        config_api = cyperf.ConfigurationsApi(self.api_client)
        config_ops = []
        for config_file in configuration_files:
            config_ops.append (config_api.start_configs_import(config_file))

        configs = []
        for op in config_ops:
            try:
                results  = op.await_completion ()
                configs += [(elem['id'], elem['configUrl']) for elem in results]
            except cyperf.ApiException as e:
                raise (e)
        return configs

    def load_configuration_file(self, configuration_file):
        configs = self.load_configuration_files ([configuration_file])
        if configs:
            return configs[0]
        else:
            return None

    def remove_configurations(self, configurations_ids=[]):
        config_api = cyperf.ConfigurationsApi(self.api_client)
        for config_id in configurations_ids:
            config_api.delete_configs (config_id)

    def remove_configuration(self, configurations_id):
        self.remove_configurations([configurations_id])

    def delete_all_configs (self):
        config_api = cyperf.ConfigurationsApi(self.api_client)
        configs    = config_api.get_configs()
        self.remove_configurations([config.id for config in configs if not config.readonly])

    def create_session_by_config_name (self, configName):
        config_api = cyperf.ConfigurationsApi(self.api_client)
        configs    = config_api.get_configs(search_col='displayName', search_val=configName)
        if not len(configs):
            return None

        return self.create_session (configs[0].config_url)

    def create_session (self, config_url):
        session_api        = cyperf.SessionsApi(self.api_client)
        session            = cyperf.Session()
        session.config_url = config_url
        sessions           = session_api.create_sessions([session])
        if len(sessions):
            return sessions[0]
        else:
            return None

    def delete_session (self, session):
        session_api = cyperf.SessionsApi(self.api_client)
        test        = session_api.get_test (session_id = session.id)
        if test.status != 'STOPPED':
            self.stop_test(session)
        session_api.delete_sessions(session.id)

    def delete_sessions (self, sessions=[]):
        session_api = cyperf.SessionsApi(self.api_client)
        for session in sessions:
            test    = session_api.get_test (session_id = session.id)
            if test.status != 'STOPPED':
                self.stop_test(session)
            session_api.delete_sessions(session.id)

    def delete_all_sessions (self):
        session_api = cyperf.SessionsApi(self.api_client)
        result      = session_api.get_sessions()
        for session in result:
            self.delete_session(session)

    def assign_agents (self, session, agent_map, augment=False):
        # Assing agents to the indivual network segments based on the input provided
        for net_profile in session.config.config.network_profiles:
            for ip_net in net_profile.ip_network_segment:
                if ip_net.name in agent_map:
                    mapped_ips    = agent_map[ip_net.name]
                    agent_details = [cyperf.AgentAssignmentDetails(agent_id = self.agents[agent_ip].id, id = self.agents[agent_ip].id) for agent_ip in mapped_ips if agent_ip in self.agents] # why do we need to pass agent_id and id both????
                    if not ip_net.agent_assignments:
                        ip_net.agent_assignments = cyperf.AgentAssignments(ByID=[], ByTag=[])

                    if augment:
                        ip_net.agent_assignments.by_id.extend(agent_details)
                    else:
                        ip_net.agent_assignments.by_id = agent_details

                    ip_net.update()

    def stop_test (self, session):
        test_ops_api = cyperf.TestOperationsApi(self.api_client)
        test_stop_op = test_ops_api.start_stop_traffic(session_id = session.id)
        try:
            test_stop_op.await_completion()
        except cyperf.ApiException as e:
            raise (e)

class Deployer(object):
    def __init__(self):
        self.terraform_dir             = './terraform'

        self.controller_admin_user     = 'admin'
        self.controller_admin_password = 'CyPerf&Keysight#1'

        self.license_server_user       = self.controller_admin_user
        self.license_server_password   = self.controller_admin_password

    def _get_utils(self, terraform_output, eula_accept_interactive):
        if 'mdw_detail' in terraform_output:
            controller     = terraform_output['mdw_detail']['value']['public_ip']
        else:
            controller     = None
        if 'license_server' in terraform_output:
            license_server = terraform_output['license_server']['value']
        else:
            license_server = None

        if not controller:
            return None

        if license_server:
            return CyPerfUtils(controller,
                               username=self.controller_admin_user,
                               password=self.controller_admin_password,
                               license_server=license_server,
                               license_user=self.license_server_user,
                               license_password=self.license_server_password,
                               eula_accept_interactive=eula_accept_interactive)
        else:
            return CyPerfUtils(controller,
                               username=self.controller_admin_user,
                               password=self.controller_admin_password,
                               eula_accept_interactive=eula_accept_interactive)

    def terraform_deploy(self):
        # cp tfvars inside ./terraform
        subprocess.run(['cp', 'terraform.tfvars', f'{self.terraform_dir}'], check=True)

        # Initialize Terraform    
        subprocess.run(['terraform', f'-chdir={self.terraform_dir}',  'init'], check=True)

        # Apply Terraform configuration
        subprocess.run(['terraform', f'-chdir={self.terraform_dir}', 'apply', '-auto-approve'], check=True)

    def collect_terraform_output(self):
        # Capture the output in JSON format
        result = subprocess.run(['terraform', f'-chdir={self.terraform_dir}', 'output', '-json'], capture_output=True, text=True, check=True)
        
        # Parse the JSON output
        terraform_output = json.loads(result.stdout)

        return terraform_output

    def terraform_destroy(self):
        # copy tfvars inside ./terraform again, the aws key information might have changed
        subprocess.run(['cp', 'terraform.tfvars', f'{self.terraform_dir}'], check=True)

        # Initialize Terraform, in case we running destroy once more
        subprocess.run(['terraform', f'-chdir={self.terraform_dir}',  'init'], check=True)

        # Destroy Terraform configuration
        subprocess.run(['terraform', f'-chdir={self.terraform_dir}', 'destroy', '-auto-approve'], check=True)

        # Remove all temporary files
        subprocess.run(['rm', '-f',  f'{self.terraform_dir}/terraform.tfvars'], check=True)
        subprocess.run(['rm', '-f',  f'{self.terraform_dir}/terraform.tfstate'], check=True)
        subprocess.run(['rm', '-f',  f'{self.terraform_dir}/terraform.tfstate.backup'], check=True)
        subprocess.run(['rm', '-f',  f'{self.terraform_dir}/.terraform.lock.hcl'], check=True)
        subprocess.run(['rm', '-rf', f'{self.terraform_dir}/.terraform/'], check=True)
        subprocess.run(['rm', '-f',  f'terraform.tfstate'], check=True)

    def deploy(self, args):
        self.terraform_deploy ()

        output = self.collect_terraform_output()
        utils  = self._get_utils(output, args.eula_accept_interactive)

        _, config_url = utils.load_configuration_file(args.config_file)
        session       = utils.create_session(config_url)

        agents = {
            'PAN-VM-FW-Client': [agent['private_ip'] for agent in output['panfw_client_agent_detail']['value']],
            'AWS-NW-FW-Client': [agent['private_ip'] for agent in output['awsfw_client_agent_detail']['value']],
            'PAN-VM-FW-Server': [agent['private_ip'] for agent in output['panfw_server_agent_detail']['value']],
            'AWS-NW-FW-Server': [agent['private_ip'] for agent in output['awsfw_server_agent_detail']['value']]
        }
        utils.assign_agents (session, agents)

        url_string = utils.color.UNDERLINE + utils.color.BLUE + f'https://{utils.controller}' + utils.color.END
        print(f'\nCyPerf controller is at: {url_string}')

    def destroy(self, args):
        output = self.collect_terraform_output()
        utils  = self._get_utils(output)
        if utils:
            utils.delete_all_sessions()
            utils.delete_all_configs()
            utils.remove_license_server()

        self.terraform_destroy ()

def parse_cli_options():
    import argparse

    parser = argparse.ArgumentParser(description='Deploy a test topology for demonstrating palo-alto firewalls.')
    parser.add_argument('--deploy',  help='Deploy all components necessary for a palo-alto firewall demonstration', action='store_true')
    parser.add_argument('--destroy', help='Cleanup all components created for the last palto-alto firewall demonstration', action='store_true')
    parser.add_argument('--config-file', help='The name of the configuration file including path', default='./configurations/Palo-Alto-Firewall-Demo.zip')
    parser.add_argument('--interactive-eula', help='If true, interactively ask for EULA authentication; if false and EULA is not accepted, will check for an env var named CYPERF_EULA_ACCEPTED.', default=False, action='store_true')
    args = parser.parse_args()

    return args

def main():
    args     = parse_cli_options()
    deployer = Deployer()

    if args.deploy:
        deployer.deploy(args)

    if args.destroy:
        deployer.destroy(args)

if __name__ == "__main__":
    main()
