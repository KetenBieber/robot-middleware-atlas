PYTHON ?= .venv/Scripts/python.exe
SPHINXBUILD ?= $(PYTHON) -m sphinx
SOURCEDIR = docs
BUILDDIR = site

.PHONY: html html-full package-site check-editorial check-baselines check-examples check-libuv check-asio check-folly check-seastar check-nginx check-libzmq check-ros1 check-ros2 check-lcm check-cyber check-ecal check-ethercat check-soem check-cyclonedds check-fastdds check-iceoryx2 check-ucx check-rosidlbuffer check-holoscan check-communication-foundations check-document-hygiene check-links clean

html:
	$(PYTHON) tools/build_sphinx_sources.py
	$(PYTHON) tools/check_document_hygiene.py
	$(PYTHON) tools/check_libuv_closure.py
	$(PYTHON) tools/check_asio_closure.py
	$(PYTHON) tools/check_folly_closure.py
	$(PYTHON) tools/check_seastar_closure.py
	$(PYTHON) tools/check_nginx_closure.py
	$(PYTHON) tools/check_libzmq_closure.py
	$(PYTHON) tools/check_ros1_closure.py
	$(PYTHON) tools/check_ros2_closure.py
	$(PYTHON) tools/check_lcm_closure.py
	$(PYTHON) tools/check_cyber_closure.py
	$(PYTHON) tools/check_ecal_closure.py
	$(PYTHON) tools/check_soem_closure.py
	$(PYTHON) tools/check_cyclonedds_closure.py
	$(PYTHON) tools/check_fastdds_closure.py
	$(PYTHON) tools/check_iceoryx2_closure.py
	$(PYTHON) tools/check_ucx_closure.py
	$(PYTHON) tools/check_rosidlbuffer_closure.py
	$(PYTHON) tools/check_holoscan_closure.py
	$(PYTHON) tools/check_communication_foundations.py
	$(PYTHON) tools/check_ethercat_closure.py
	$(PYTHON) tools/check_source_baselines.py
	$(PYTHON) tools/check_editorial_language.py
	$(PYTHON) -c "import shutil; shutil.rmtree(r'$(BUILDDIR)', ignore_errors=True)"
	$(SPHINXBUILD) -b html -W --keep-going $(SOURCEDIR) $(BUILDDIR)
	$(PYTHON) tools/check_editorial_language.py --site

html-full:
	$(PYTHON) tools/build_sphinx_sources.py
	$(PYTHON) tools/check_document_hygiene.py
	$(PYTHON) tools/check_libuv_closure.py
	$(PYTHON) tools/check_asio_closure.py
	$(PYTHON) tools/check_folly_closure.py
	$(PYTHON) tools/check_seastar_closure.py
	$(PYTHON) tools/check_nginx_closure.py
	$(PYTHON) tools/check_libzmq_closure.py
	$(PYTHON) tools/check_ros1_closure.py
	$(PYTHON) tools/check_ros2_closure.py
	$(PYTHON) tools/check_lcm_closure.py
	$(PYTHON) tools/check_cyber_closure.py
	$(PYTHON) tools/check_ecal_closure.py
	$(PYTHON) tools/check_soem_closure.py
	$(PYTHON) tools/check_cyclonedds_closure.py
	$(PYTHON) tools/check_fastdds_closure.py
	$(PYTHON) tools/check_iceoryx2_closure.py
	$(PYTHON) tools/check_ucx_closure.py
	$(PYTHON) tools/check_rosidlbuffer_closure.py
	$(PYTHON) tools/check_holoscan_closure.py
	$(PYTHON) tools/check_communication_foundations.py
	$(PYTHON) tools/check_ethercat_closure.py
	$(PYTHON) tools/check_source_baselines.py
	$(PYTHON) tools/check_editorial_language.py
	$(PYTHON) tools/check_cpp_examples.py
	$(PYTHON) -c "import shutil; shutil.rmtree(r'$(BUILDDIR)', ignore_errors=True)"
	$(SPHINXBUILD) -E -a -b html -W --keep-going $(SOURCEDIR) $(BUILDDIR)
	$(PYTHON) tools/check_editorial_language.py --site
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

check-libuv:
	$(PYTHON) tools/build_sphinx_sources.py
	$(PYTHON) tools/check_libuv_closure.py

check-asio:
	$(PYTHON) tools/build_sphinx_sources.py
	$(PYTHON) tools/check_asio_closure.py

check-folly:
	$(PYTHON) tools/build_sphinx_sources.py
	$(PYTHON) tools/check_folly_closure.py

check-nginx:
	$(PYTHON) tools/build_sphinx_sources.py
	$(PYTHON) tools/check_nginx_closure.py

check-libzmq:
	$(PYTHON) tools/build_sphinx_sources.py
	$(PYTHON) tools/check_libzmq_closure.py

check-ros1:
	$(PYTHON) tools/build_sphinx_sources.py
	$(PYTHON) tools/check_ros1_closure.py

check-ros2:
	$(PYTHON) tools/build_sphinx_sources.py
	$(PYTHON) tools/check_ros2_closure.py

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

check-ucx:
	$(PYTHON) tools/check_ucx_closure.py

check-rosidlbuffer:
	$(PYTHON) tools/build_sphinx_sources.py
	$(PYTHON) tools/check_rosidlbuffer_closure.py

check-holoscan:
	$(PYTHON) tools/build_sphinx_sources.py
	$(PYTHON) tools/check_holoscan_closure.py

check-communication-foundations:
	$(PYTHON) tools/check_communication_foundations.py
check-document-hygiene:
	$(PYTHON) tools/build_sphinx_sources.py
	$(PYTHON) tools/check_document_hygiene.py
check-cyclonedds:
	$(PYTHON) tools/check_cyclonedds_closure.py

check-ecal:
	$(PYTHON) tools/check_ecal_closure.py
	$(PYTHON) tools/check_cpp_examples.py

check-ethercat:
	$(PYTHON) tools/check_ethercat_closure.py

clean:
	$(SPHINXBUILD) -M clean $(SOURCEDIR) $(BUILDDIR)
