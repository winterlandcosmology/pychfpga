{% if modules or module == name %}
{% set objtype = "Package" %}
{% else %}
{% set objtype = "Module" %}
{% endif %}

{% if modules or module == name %}
{{ (":py:mod:`" + name + "/ <" + fullname + ">`") | underline }}
{% else %}
{{ (":py:mod:`" + name + ".py <" + fullname + ">`") | underline }}
{% endif %}

.. currentmodule:: {{ module }}

.. .. note::

..    | template = custom_module-template.rst
..    | Module={{ module }},
..    | FULLNAME={{ fullname }},
..    | name=={{ name }}
..    | modules = {{ modules }}
..    | classes = {{ classes }}

.. automodule:: {{ fullname }}

   .. *** Insert submodule summary

   {% block modules %}
   {# GUI app: list the package, but do not walk its Qt modules. #}
   {% if fullname.startswith('pychfpga.apps.ber_dashboard') %}
   {% set visible_modules = [] %}
   {% else %}
   {% set visible_modules = modules %}
   {% endif %}
   {% if visible_modules %}
   .. container:: autosummary_block

      .. rubric:: {{ objtype }} sub-modules

      .. autosummary::
         :toctree:
         :template: custom-module-template.rst
         :recursive:

         {% for item in visible_modules %}
         ~{{ item }}
         {%- endfor %}
   {% endif %}
   {% endblock %}

   .. *** Insert attribute summary

   {% block attributes %}
   {% if attributes %}

   .. container:: autosummary_block

      .. rubric:: Module attributes summary

      .. autosummary::
         :nosignatures:

         {% for item in attributes %}
         {{ item }}
         {%- endfor %}
   {% endif %}
   {% endblock %}

   .. *** Insert exception summary

   {% block exceptions %}
   {% if exceptions %}
   .. container:: autosummary_block

      .. rubric:: Module exceptions summary

      .. autosummary::
         :nosignatures:

         {% for item in exceptions %}
         {{ item }}
         {%- endfor %}
   {% endif %}
   {% endblock %}


   .. *** Insert function summary

   {% block functions %}
   {% if functions %}

   .. container:: autosummary_block

      .. rubric:: Module functions summary

      .. autosummary::
         :nosignatures:

         {% for item in functions %}
         {{ item }}
         {%- endfor %}

   {% endif %}
   {% endblock %}

   .. *** Insert class summary

   {% block classes %}
   {% if classes %}

   .. container:: autosummary_block

      .. rubric:: Module classes summary

      .. autosummary::
         {#- :template: custom-class-template.rst #}
         :nosignatures:

         {% for item in classes %}
         {{ item }}
         {%- endfor %}
   {% endif %}
   {% endblock %}



.. *** Insert attribute descriptions


{% if attributes %}
----

.. rubric:: Module attributes

{% for item in attributes %}
.. autoattribute:: {{ item }}

{% endfor %}
{% endif %}


.. *** Insert exceptions descriptions


{% if exceptions %}
----

.. rubric:: Exceptions

{% for item in exceptions %}
.. autoexception:: {{ item }}

{% endfor %}
----
{% endif %}

.. *** Insert function descriptions

{% if functions %}
----

.. rubric:: Module functions

{% for item in functions %}
.. autofunction:: {{ item }}

{% endfor %}
{% endif %}


.. *** Insert class descriptions


{% if classes %}
----

.. rubric:: Module classes

{% for item in classes %}
.. autoclass:: {{ item }}
   :members:
   :undoc-members:
   :show-inheritance:
   :inherited-members:
   :special-members: __call__, __init__

{% endfor %}
{% endif %}
