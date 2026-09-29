PYTHON ?= .venv/Scripts/python.exe
SPHINXBUILD ?= $(PYTHON) -m sphinx
SOURCEDIR = docs
BUILDDIR = site

.PHONY: html html-full package-site check-editorial check-baselines check-examples check-lcm check-cyber check-ecal check-ethercat check-soem check-cyclonedds check-fastdds check-iceoryx2 check-communication-foundations check-links clean

html:
	$(PYTHON) tools/build_sphinx_sources.py
	$(PYTHON) tools/check_lcm_closure.py
	$(PYTHON) tools/check_cyber_closure.py
	$(PYTHON) tools/check_ecal_closure.py
	$(PYTHON) tools/check_soem_closure.py
	$(PYTHON) tools/check_cyclonedds_closure.py
	$(PYTHON) tools/check_fastdds_closure.py
	$(PYTHON) tools/check_iceoryx2_closure.py
	$(PYTHON) tools/check_communication_foundations.py
	$(PYTHON) tools/check_ethercat_closure.py
	$(PYTHON) tools/check_source_baselines.py
	$(PYTHON) tools/check_editorial_language.py
	$(SPHINXBUILD) -b html -W --keep-going $(SOURCEDIR) $(BUILDDIR)

html-full:
	$(PYTHON) tools/build_sphinx_sources.py
	$(PYTHON) tools/check_lcm_closure.py
	$(PYTHON) tools/check_cyber_closure.py
	$(PYTHON) tools/check_ecal_closure.py
	$(PYTHON) tools/check_soem_closure.py
	$(PYTHON) tools/check_cyclonedds_closure.py
	$(PYTHON) tools/check_fastdds_closure.py
	$(PYTHON) tools/check_iceoryx2_closure.py
	$(PYTHON) tools/check_communication_foundations.py
	$(PYTHON) tools/check_ethercat_closure.py
	$(PYTHON) tools/check_source_baselines.py
	$(PYTHON) tools/check_editorial_language.py
	$(PYTHON) tools/check_cpp_examples.py
	$(SPHINXBUILD) -E -a -b html -W --keep-going $(SOURCEDIR) $(BUILDDIR)
	$(PYTHON) tools/check_static_links.py $(BUILDDIR)

check-links:
	$(PYTHON) tools/check_static_links.py $(BUILDDIR)

package-site:
	$(PYTHON) tools/package_site.py

check-editorial:
	$(PYTHON) tools/check_editorial_language.py

check-baselines:
	$(PYTHON) tools/check_source_baselines.py

check-examples:
	$(PYTHON) tools/check_cpp_examples.py

check-lcm:
	$(PYTHON) tools/check_lcm_closure.py
	$(PYTHON) tools/check_cpp_examples.py

check-cyber:
	$(PYTHON) tools/check_cyber_closure.py
check-soem:
	$(PYTHON) tools/check_soem_closure.py


check-fastdds:
	$(PYTHON) tools/check_fastdds_closure.py

check-iceoryx2:
	$(PYTHON) tools/check_iceoryx2_closure.py

check-communication-foundations:
	$(PYTHON) tools/check_communication_foundations.py
check-cyclonedds:
	$(PYTHON) tools/check_cyclonedds_closure.py

check-ecal:
	$(PYTHON) tools/check_ecal_closure.py
	$(PYTHON) tools/check_cpp_examples.py

check-ethercat:
	$(PYTHON) tools/check_ethercat_closure.py

clean:
	$(SPHINXBUILD) -M clean $(SOURCEDIR) $(BUILDDIR)
