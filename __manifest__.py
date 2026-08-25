{
    'name': 'Database Data Wipe',
    'summary': "Tool to Completely Clear All Data from the Odoo Database",
    'description': """
Database Data Wipe
==================
- This module provides an option to completely remove all data from your Odoo database, including records from all models.
- It is especially useful when resetting a development or testing environment to a clean state.
- This action is irreversible. All data in the database will be permanently deleted.
- Use this module with extreme caution, and only when a full reset is absolutely required.
    """,
    "version": "18.0.1.0.2",
    "author": "KoderXpert Technologies Private Limited",
    "company": "KoderXpert Technologies Private Limited",
    "maintainer": "KoderXpert Technologies Private Limited",
    "website": "https://koderxpert.com",
    "category": "Tools",
    "data": ['views/res_config_settings_view.xml'],
    'license': 'OPL-1',
    'installable': True,
    'application': True,
    'auto_install': False,
    "images":['static/description/data_elimination.gif'],
}
