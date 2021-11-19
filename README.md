Deploy-Odoo
=========

This role is for deploying and managing Odoo instances with different versions.
It is intended to be used with the Ansible Tower and the Xayma.sh Platform already deployed (with all it's components).  
However if you want to use command create and manage odoo instances from the command line, with the Xayma.sh Platform already deployed, you can use the following:

```bash
ansible-playbook site.yml -i production --tags "deployodoo" --extra-vars "organization=xaymasolutions instancename=portal domain=portal.xaymasolutions.com" --vault-pass-file "vault_password" -K
```

> You need to know that when using the CLI way, the instance's addon folder will not be created by default and it might lead to some errors. In that situation you will need to create the addon folder manually.


Requirements
------------
- Deployment of the Xayma.sh Platform. Otherwise this is completely useless.
- Make sure to use the secret vault password 😎. Can be a file (if you are using CLI) or a credential record in Ansible Tower.


Role Tags
---------
You can use this role with the following tags: 

| Tag                    | Description              |
|------------------------|--------------------------|
| deployodoo             | To create a new instance of Odoo      |
| stopinstance           | To stop the instance     |
| startinstance          | To start the instance    |
| suspendinstance        | To suspend the instance (stop and display a "suspended" page in the browser |
| editinstancedomainname | To change the main domain name of the instance   |


Role Variables
--------------

You can customize you instance by using thes variables
| Tag                    | Description              |
|------------------------|--------------------------|
| organization           | The customer for which the instance is being created (*should be one single word with no special characters*) |
| instancename           | The name of the instance (*should be one single word with no special characters*)  |
| domain                 | The domain name to which the instance will be bound |
| version                | The version Odoo that will be deployed (*Should be an existing version*) |


Dependencies
------------
All dependencies should be already installed during the deployment of the Xayma.sh platform.

License
-------

MIT

Author Information
------------------

- Elhadji Malang Diedhiou  
For the past seve years I have been helping businesses to increase efficiency, using automation tools. I am passionate in learning and sharing.  
**More about me**:
  * [LinkedIn]
  * [Twitter]
  * [GitHub]

[LinkedIn]: https://linkedin.com/in/supermalang
[GitHub]: https://github.com/supermalang
[Twitter]: https://twitter.com/supermalang_

