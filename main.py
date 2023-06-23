from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from passwd import username, password
import pandas as pd
import time
import openpyxl
import random


class LinkedInJobScraper:
    def __init__(self, file_path):
        self.driver = None
        self.username = username
        self.password = password
        self.jobs_df = None
        self.file_path = file_path

    def load_jobs_from_file(self, file_path):
        try:
            self.jobs_df = pd.read_excel(file_path)
        except FileNotFoundError:
            self.jobs_df = pd.DataFrame(columns=['Title', 'Company', 'Location', 'Link', 'Keywords', 'Applied', 'Date_posted'])

    def save_jobs_to_file(self):
        self.jobs_df.to_excel((self.file_path), index=False)

    def login(self):
        self.driver.get("https://www.linkedin.com/login/pt?fromSignIn=true&trk=guest_homepage-basic_nav-header-signin")
        self.driver.find_element(By.ID, "username").send_keys(self.username)
        self.driver.find_element(By.ID, "password").send_keys(self.password)
        self.driver.find_element(By.XPATH, "/html/body/div/main/div/div/form/div/button").click()
        time.sleep(15)

    def get_url(self, job_name, country_name):
        job_url = "%20".join(job_name.split())
        country_url = "%20".join(country_name.split())
        url = f"https://www.linkedin.com/jobs/search?keywords={job_url}&location={country_url}&geoId=103644278&trk=public_jobs_jobs-search-bar_search-submit&f_WT=2&position=1&pageNum=0"
        print(url)
        return url

    def initialize_driver(self):
        self.driver = webdriver.Chrome()
        self.driver.maximize_window()

    def scrape_jobs(self, job_name, country_name):
        url = self.get_url(job_name, country_name)
        self.driver.get(url)
        time.sleep(2)

        actual = 1
        while actual:
            jobs_result_list = self.driver.find_element(By.CLASS_NAME, "jobs-search-results-list")
            self.driver.execute_script("arguments[0].scrollTop = arguments[0].scrollHeight;", jobs_result_list)
            time.sleep(1 + random.random())
            jobs = self.driver.find_elements(By.XPATH, "//ul[@class='scaffold-layout__list-container']/li")
            for i in range(len(jobs)):
                print("Lendo "+str(i+1)+"/"+str(len(jobs)+1))
                actions = ActionChains(self.driver)
                actions.move_to_element(jobs[i]).click().perform()
                time.sleep(1 + random.random())

                jd_element = self.driver.find_element(By.XPATH, "//div[@id='job-details']/span")
                jd_text = jd_element.get_attribute("innerText")

                if not self.contains_bad_keywords(jd_text) and self.contains_good_keywords(jd_text):
                    print("Encontrado 1 job")
                    job_title = self.driver.find_element(By.XPATH, "//h2[@class='t-24 t-bold jobs-unified-top-card__job-title']").get_attribute("innerText")
                    if job_name in job_title:
                        keywords_text = [word for word in keywords_good if word.lower() in jd_text.lower()]
                        company_name = self.driver.find_element(By.XPATH, "//a[@class='ember-view t-black t-normal']").get_attribute("innerText")
                        location = self.driver.find_element(By.XPATH, "//span[@class='jobs-unified-top-card__subtitle-primary-grouping t-black']/span[2]").get_attribute("innerText")
                        job_link = self.driver.find_element(By.XPATH, "//div[@class='display-flex justify-space-between']/a").get_attribute("href")

                        applied = "YES" if self.driver.find_elements(By.XPATH, "//span[@class='artdeco-inline-feedback__message']") else ''

                        if jobs[i].find_elements(By.XPATH, ".//ul[@class='job-card-list__footer-wrapper job-card-container__footer-wrapper flex-shrink-zero display-flex t-sans t-12 t-black--light t-normal t-roman']/li/time"):
                            date_posted = jobs[i].find_element(By.XPATH, ".//ul[@class='job-card-list__footer-wrapper job-card-container__footer-wrapper flex-shrink-zero display-flex t-sans t-12 t-black--light t-normal t-roman']/li/time").get_attribute("datetime")
                        else:
                            date_posted = ""

                        df = pd.DataFrame({
                            'Title': job_title,
                            'Company': company_name,
                            'Location': location,
                            'Link': job_link,
                            'Keywords': ' '.join(keywords_text),
                            'Description': jd_text,
                            'Applied': applied,
                            'Date_posted': date_posted
                        }, index=['Link'])

                        self.jobs_df = pd.concat([self.jobs_df, df])

                time.sleep(0.5 + random.random())

            self.save_jobs_to_file()
            if self.driver.find_elements(By.XPATH, "//li[@data-test-pagination-page-btn='"+str(actual+1)+"']"):
                actual = actual+1
                next_page = self.driver.find_element(By.XPATH, "//li[@data-test-pagination-page-btn='"+str(actual)+"']")
                next_page.click()
            else:
                actual = None

    def scroll_to_end(self):
        self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")

    def contains_bad_keywords(self, text):
        return any(word.lower() in text.lower() for word in keywords_bad)

    def contains_good_keywords(self, text):
        return any(word.lower() in text.lower() for word in keywords_good)

    def get_total_jobs(self):
        try:
            subtitle = self.driver.find_element(By.XPATH, "//div[@class='jobs-search-results-list__subtitle']/span")
            jobs_num = int(subtitle.get_attribute("innerText").replace(",", "").replace(".", "").replace(" resultados", ""))
            return jobs_num
        except:
            return 0

    def run(self, job_name, country_name):
        self.initialize_driver()
        self.load_jobs_from_file(self.file_path)
        self.login()
        self.scrape_jobs(job_name, country_name)
        self.driver.quit()


# Set the job_name and the Country to use in search params
job_name = "Data Engineer"
country_name = "Worldwide"

# Destiny path to the output
file_path = "jobs.xlsx"

# Set up keywords
keywords_bad = [
    "software engineer",
    "hybrid",
    "anywhere in the u.s",
    "residing in u.s",
    "remote in the united states",
    "us location",
    "located in the us",
    "residing",
    "w2 only",
    "work permit",
    "located within the U.S",
    "within the U.S",
    "us/canada residing",
    "us resid",
    "clt",
    "no C2C",
    "Anywhere in USA",
    "anywhere in the EST in the US",
    "within the US",
    "Remote - UK",
    "located in India",
    "anywhere (in Europe)",
    "within Germany",
    "anywhere in the Philippines",
    "anywhere in Australia"
]

keywords_good = [
    "fully remote",
    "remote first",
    "anywhere",
    "remote-first",
    "c2c",
    "contractor",
    "latam",
    "brazil",
    "brasil",
    "li-remote",
    "work from home",
    "llc",
    "wfh",
    "100% remote",
    "within Latin America",
    "within latam",
    "payment in usd",
    "Latin America"
]

# Init the code
scraper = LinkedInJobScraper(file_path)
scraper.run(job_name, country_name)
