# concat datasets to articles.csv

import pandas as pd

df1 = pd.read_csv("dawn_intl_iran_usa_filtered.csv")
df2 = pd.read_csv("guardian_news_iran_usa_filtered.csv")

df = pd.concat([df1, df2], ignore_index=True)
df.to_csv("articles.csv", index=False)